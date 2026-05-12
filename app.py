from flask import Flask, render_template, jsonify, request
import requests as req_lib
import json, os, time, logging, logging.handlers
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
import ipaddress, secrets

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('NEXRAD_SECRET_KEY') or secrets.token_hex(32)

# ── Paths ────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, 'settings.json')
LOG_FILE      = os.path.join(BASE_DIR, 'logs', 'alerts.jsonl')
NOTIFIED_FILE = os.path.join(BASE_DIR, 'logs', 'notified.json')
os.makedirs(os.path.join(BASE_DIR, 'logs'), exist_ok=True)

HEADERS = {"User-Agent": "OrlandoWeatherRadar/1.0"}

# ── Radar stations ───────────────────────────────────────
RADAR_PRIMARY       = 'KMLB'
RADAR_SECONDARY     = 'KTBW'
RADAR_STALE_MINUTES = 15

STATION_COORDS = {
    'KMLB': {'lat': 28.1128, 'lon': -80.6547, 'name': 'Melbourne FL'},
    'KTBW': {'lat': 27.7056, 'lon': -82.4019, 'name': 'Tampa Bay FL'},
}

VCP_INFO = {
    '12':  {'seconds': 250, 'mode': 'Precipitation', 'label': '~4 min'},
    '212': {'seconds': 250, 'mode': 'Precipitation', 'label': '~4 min'},
    '112': {'seconds': 270, 'mode': 'Precipitation', 'label': '~4.5 min'},
    '115': {'seconds': 310, 'mode': 'Precipitation', 'label': '~5 min'},
    '35':  {'seconds': 370, 'mode': 'Precipitation', 'label': '~6 min'},
    '215': {'seconds': 370, 'mode': 'Precipitation', 'label': '~6 min'},
    '31':  {'seconds': 610, 'mode': 'Clear Air',     'label': '~10 min'},
    '32':  {'seconds': 610, 'mode': 'Clear Air',     'label': '~10 min'},
}

OFFLINE_STATUSES = {
    'RDA_OFFLINE', 'MAINTENANCE_REQUIRED', 'INOPERABLE',
    'WIDEBAND_DISCONNECT', 'COMMANDED_SHUTDOWN',
}

SEV_ORDER = {'Extreme': 4, 'Severe': 3, 'Moderate': 2, 'Minor': 1, 'Unknown': 0}

# ── Default settings ─────────────────────────────────────
DEFAULT_SETTINGS = {
    'radar_source':         'rainviewer',
    'rv_color':             6,
    'opacity':              70,
    'anim_speed':           500,
    'show_sidebar':         True,
    'map_zoom':             8,
    'map_lat':              28.5383,
    'map_lon':             -81.3792,
    # Overlays
    'show_alert_polygons':  True,
    'show_range_rings':     False,
    'range_ring_station':   'KMLB',
    'show_satellite_ir':    False,  # RainViewer infrared satellite overlay
    'show_hurricane':       True,
    'nws_product':          'conus_bref_qcd',
    # Notifications
    'webhook_url':            '',
    'webhook_min_severity':   'Severe',
    # Syslog
    'syslog_enabled':   False,
    'syslog_host':      '',
    'syslog_port':      514,
    'syslog_facility':  'local0',
    'updated_at': 0,
}

# ── Settings helpers ─────────────────────────────────────
ALLOWED_RADAR_SOURCES    = {'rainviewer', 'nws'}
ALLOWED_NWS_PRODUCTS     = {'conus_bref_qcd','conus_cref_qcd','conus_bvel_qcd',
                             'conus_n1p_qcd','conus_ntp_qcd'}
ALLOWED_RING_STATIONS    = {'KMLB','KTBW'}
ALLOWED_SEVERITIES       = {'Extreme','Severe','Moderate','Minor'}
ALLOWED_SYSLOG_FACILITIES= {'local0','local1','local2','local3','local4',
                             'local5','local6','local7','user','daemon'}

def _is_safe_url(url):
    """Block SSRF: only http/https, no private/loopback IPs."""
    try:
        p = urlparse(url)
        if p.scheme not in ('http','https') or not p.hostname:
            return False
        try:
            ip = ipaddress.ip_address(p.hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return False
        except ValueError:
            pass  # hostname, not an IP — allow
        return True
    except Exception:
        return False

def validate_settings(data):
    """Coerce and validate settings values; raises ValueError on bad input."""
    out = {}
    def fi(k, mn, mx):
        if k in data:
            try: out[k]=max(mn,min(mx,float(data[k])))
            except (TypeError,ValueError): raise ValueError(f'{k} must be a number between {mn} and {mx}')
    def ii(k, mn, mx):
        if k in data:
            try: out[k]=max(mn,min(mx,int(data[k])))
            except (TypeError,ValueError): raise ValueError(f'{k} must be an integer between {mn} and {mx}')
    def en(k, allowed):
        if k in data:
            v=str(data[k])
            if v not in allowed: raise ValueError(f'{k} must be one of: {", ".join(sorted(allowed))}')
            out[k]=v
    fi('map_lat',-90,90); fi('map_lon',-180,180)
    ii('map_zoom',2,18); ii('opacity',0,100); ii('anim_speed',50,5000)
    ii('rv_color',0,8); ii('syslog_port',1,65535)
    en('radar_source',ALLOWED_RADAR_SOURCES); en('nws_product',ALLOWED_NWS_PRODUCTS)
    en('range_ring_station',ALLOWED_RING_STATIONS); en('webhook_min_severity',ALLOWED_SEVERITIES)
    en('syslog_facility',ALLOWED_SYSLOG_FACILITIES)
    for k in ('show_sidebar','show_alert_polygons','show_range_rings',
              'show_satellite_ir','show_hurricane','syslog_enabled'):
        if k in data: out[k]=bool(data[k])
    if 'webhook_url' in data:
        url=str(data['webhook_url']).strip()[:2048]
        if url and not _is_safe_url(url):
            raise ValueError('Webhook URL must use http/https and must not target a private/loopback address')
        out['webhook_url']=url
    if 'syslog_host' in data:
        out['syslog_host']=str(data['syslog_host']).strip()[:253]
    return {**data,**out}


def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE) as f:
                return {**DEFAULT_SETTINGS, **json.load(f)}
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f'load_settings failed, using defaults: {e}')
    return DEFAULT_SETTINGS.copy()

def save_settings(data):
    data = validate_settings(data)
    current = load_settings()
    allowed = set(DEFAULT_SETTINGS.keys()) - {'updated_at'}
    current.update({k: v for k, v in data.items() if k in allowed})
    current['updated_at'] = time.time()
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(current, f, indent=2)
    setup_syslog(current)
    log_event('SETTINGS_CHANGED', {})
    return current

# ── Syslog ───────────────────────────────────────────────
app_logger = logging.getLogger('nexrad')
app_logger.setLevel(logging.INFO)

def setup_syslog(settings=None):
    if settings is None:
        settings = load_settings()
    app_logger.handlers = [
        h for h in app_logger.handlers
        if not isinstance(h, logging.handlers.SysLogHandler)
    ]
    if not settings.get('syslog_enabled') or not settings.get('syslog_host'):
        return
    fac_attr = f"LOG_{settings.get('syslog_facility','local0').upper()}"
    facility = getattr(logging.handlers.SysLogHandler, fac_attr,
                       logging.handlers.SysLogHandler.LOG_LOCAL0)
    try:
        h = logging.handlers.SysLogHandler(
            address=(settings['syslog_host'], int(settings.get('syslog_port', 514))),
            facility=facility,
        )
        h.setFormatter(logging.Formatter('nexrad-radar: %(message)s'))
        app_logger.addHandler(h)
        app_logger.info('Syslog handler initialized')
    except Exception as e:
        app.logger.error(f'Syslog setup failed: {e}')

setup_syslog()

# ── Event log ────────────────────────────────────────────
def log_event(event_type, props):
    entry = {
        'ts':       datetime.now(timezone.utc).isoformat(),
        'type':     event_type,
        'event':    props.get('event', ''),
        'severity': props.get('severity', ''),
        'headline': props.get('headline', ''),
        'area':     props.get('areaDesc', ''),
        'id':       props.get('id', ''),
    }
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception as e:  # noqa: BLE001
        app.logger.error(f'log_event write failed: {e}')
    sev = props.get('severity', '')
    evt = props.get('event', '')
    area = props.get('areaDesc', '')
    if event_type in ('NEW_ALERT', 'CLEARED_ALERT'):
        app_logger.info(f'{event_type} severity="{sev}" event="{evt}" area="{area}"')
    elif event_type == 'STATION_FAILOVER':
        app_logger.warning(f'STATION_FAILOVER station="{props.get("station","")}" reason="{evt}"')
    elif event_type == 'SERVER_START':
        app_logger.info('SERVER_START')
    elif event_type != 'SETTINGS_CHANGED':
        app_logger.info(event_type)

# ── Webhook ──────────────────────────────────────────────
def load_notified():
    try:
        if os.path.exists(NOTIFIED_FILE):
            with open(NOTIFIED_FILE) as f:
                data = json.load(f)
            cutoff = time.time() - 48 * 3600
            return {k: v for k, v in data.items() if v > cutoff}
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f'load_notified failed: {e}')
    return {}

def save_notified(d):
    try:
        with open(NOTIFIED_FILE, 'w') as f:
            json.dump(d, f)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f'save_notified failed: {e}')

def fire_webhook(settings, props):
    url = settings.get('webhook_url', '').strip()
    if not url:
        return
    min_sev = SEV_ORDER.get(settings.get('webhook_min_severity', 'Severe'), 3)
    if SEV_ORDER.get(props.get('severity', ''), 0) < min_sev:
        return
    try:
        req_lib.post(url, json={
            'text':     f"\u26a0\ufe0f {props.get('severity')} \u2013 {props.get('event')}",
            'event':    props.get('event'),
            'severity': props.get('severity'),
            'headline': props.get('headline'),
            'area':     props.get('areaDesc'),
            'expires':  props.get('expires'),
        }, timeout=5)
        app_logger.info(f'Webhook fired: {props.get("event")}')
    except Exception as e:
        app_logger.error(f'Webhook failed: {e}')

# ── Alert state ───────────────────────────────────────────
_last_alert_ids: set = set()

def process_alert_changes(features, settings):
    global _last_alert_ids
    current = {
        f['properties']['id']: f['properties']
        for f in features
        if f.get('properties', {}).get('id')
    }
    notified = load_notified()
    changed  = False

    for aid, props in current.items():
        if aid not in _last_alert_ids:
            log_event('NEW_ALERT', props)
        if aid not in notified:
            fire_webhook(settings, props)
            notified[aid] = time.time()
            changed = True

    for aid in _last_alert_ids - set(current.keys()):
        log_event('CLEARED_ALERT', {'id': aid})

    if changed:
        save_notified(notified)
    _last_alert_ids = set(current.keys())

# ── Page routes ───────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/output')
def output():
    return render_template('output.html')

@app.route('/settings')
def settings_page():
    return render_template('settings.html',
                           s=load_settings(),
                           station_coords=json.dumps(STATION_COORDS))

# ── API: core ─────────────────────────────────────────────
@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'time': time.time()})

@app.route('/api/settings', methods=['GET'])
def api_settings_get():
    return jsonify(load_settings())

@app.route('/api/settings', methods=['POST'])
def api_settings_post():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400
    try:
        saved = save_settings(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(saved)

@app.route('/api/station_coords')
def api_station_coords():
    return jsonify(STATION_COORDS)

# ── API: alerts ───────────────────────────────────────────
def _alerts_for_point(lat, lon):
    resp = req_lib.get(
        f'https://api.weather.gov/alerts/active?point={lat},{lon}',
        headers=HEADERS, timeout=10,
    )
    resp.raise_for_status()
    return resp.json()

@app.route('/api/alerts')
def get_alerts():
    s = load_settings()
    try:
        data = _alerts_for_point(s.get('map_lat', 28.5383), s.get('map_lon', -81.3792))
        process_alert_changes(data.get('features', []), s)
        return jsonify(data)
    except req_lib.RequestException as e:
        app_logger.error(f'Alerts fetch failed: {e}')
        return jsonify({'error': 'Alert service temporarily unavailable', 'features': []}), 500

@app.route('/api/alert_polygons')
def get_alert_polygons():
    s = load_settings()
    try:
        data     = _alerts_for_point(s.get('map_lat', 28.5383), s.get('map_lon', -81.3792))
        features = []
        for f in data.get('features', []):
            if not f.get('geometry'):
                continue
            features.append({
                'type': 'Feature',
                'geometry': f['geometry'],
                'properties': {
                    'event':    f['properties'].get('event', ''),
                    'severity': f['properties'].get('severity', 'Unknown'),
                    'headline': f['properties'].get('headline', ''),
                },
            })
        return jsonify({'type': 'FeatureCollection', 'features': features})
    except Exception as e:
        return jsonify({'type': 'FeatureCollection', 'features': [], 'error': str(e)})

# ── API: radar ────────────────────────────────────────────
@app.route('/api/rainviewer')
def get_rainviewer():
    try:
        resp = req_lib.get('https://api.rainviewer.com/public/weather-maps.json', timeout=10)
        resp.raise_for_status()
        return jsonify(resp.json())
    except req_lib.RequestException as e:
        return jsonify({'error': str(e)}), 500

# ── API: hurricane ────────────────────────────────────────
@app.route('/api/hurricane')
def get_hurricane():
    try:
        resp = req_lib.get('https://www.nhc.noaa.gov/CurrentStormList.json',
                           headers=HEADERS, timeout=8)
        resp.raise_for_status()
        storms   = resp.json()
        atlantic = [s for s in storms if s.get('basin') in ('al', 'ep')]
        return jsonify({'storms': atlantic, 'count': len(atlantic)})
    except Exception as e:
        return jsonify({'storms': [], 'count': 0, 'error': str(e)})

# ── API: logs ─────────────────────────────────────────────
@app.route('/api/logs')
def get_logs():
    n = min(int(request.args.get('n', 200)), 500)
    entries = []
    try:
        if os.path.exists(LOG_FILE):
            lines   = open(LOG_FILE).readlines()
            entries = [json.loads(l) for l in lines[-n:] if l.strip()]
            entries.reverse()
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f'get_logs read failed: {e}')
    return jsonify({'entries': entries})

@app.route('/api/logs/clear', methods=['POST'])
def clear_logs():
    try:
        open(LOG_FILE, 'w').close()
        app_logger.info('Alert log cleared')
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Radar station health ──────────────────────────────────
def _fetch_station(sid):
    try:
        resp = req_lib.get(f'https://api.weather.gov/radar/stations/{sid}',
                           headers=HEADERS, timeout=8)
        resp.raise_for_status()
        props     = resp.json().get('properties', {})
        rda       = props.get('rda', {}) or {}
        rda_props = rda.get('properties', {}) if isinstance(rda, dict) else {}
        vcp       = str(rda_props.get('volumeCoveragePattern', '') or '').strip()
        op_status = rda_props.get('operabilityStatus', 'UNKNOWN')
        ts_str    = rda.get('timestamp') if isinstance(rda, dict) else None
        age       = None
        if ts_str:
            try:
                ts  = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                age = (datetime.now(timezone.utc) - ts).total_seconds() / 60
            except ValueError:
                pass
        online = (op_status not in OFFLINE_STATUSES) and (age is not None) and (age < RADAR_STALE_MINUTES)
        return {'station': sid, 'vcp': vcp or None, 'op_status': op_status,
                'age_minutes': round(age, 1) if age is not None else None, 'online': online}
    except Exception as e:
        return {'station': sid, 'online': False, 'error': str(e)}

_last_active_station = RADAR_PRIMARY

@app.route('/api/radar_status')
def radar_status():
    global _last_active_station
    results = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        for f in as_completed({ex.submit(_fetch_station, s): s
                               for s in [RADAR_PRIMARY, RADAR_SECONDARY]}):
            r = f.result()
            results[r['station']] = r

    chosen, fallback = None, False
    for sid in [RADAR_PRIMARY, RADAR_SECONDARY]:
        if results.get(sid, {}).get('online'):
            chosen, fallback = results[sid], (sid != RADAR_PRIMARY)
            break
    if chosen is None:
        chosen = results.get(RADAR_PRIMARY, {'station': RADAR_PRIMARY})

    active = chosen['station']
    if active != _last_active_station:
        log_event('STATION_FAILOVER', {'station': active,
                  'event': f'Switched from {_last_active_station} to {active}'})
    _last_active_station = active

    vcp  = chosen.get('vcp')
    info = VCP_INFO.get(str(vcp or ''), {'seconds': 300, 'mode': 'Unknown', 'label': '~5 min'})
    return jsonify({
        'station': active, 'vcp': vcp,
        'operability_status':    chosen.get('op_status', 'UNKNOWN'),
        'age_minutes':           chosen.get('age_minutes'),
        'scan_interval_seconds': info['seconds'],
        'mode': info['mode'], 'label': info['label'],
        'fallback': fallback,
        'offline':  not chosen.get('online', False),
        'stations': {
            s: {'online': d.get('online', False), 'op_status': d.get('op_status'),
                'age_minutes': d.get('age_minutes'), 'vcp': d.get('vcp'),
                'error': d.get('error')}
            for s, d in results.items()
        },
    })

# ── Init ──────────────────────────────────────────────────
log_event('SERVER_START', {'event': 'Flask server started'})

if __name__ == '__main__':
    debug = os.environ.get('NEXRAD_DEBUG','').lower() in ('1','true','yes')
    app.run(debug=debug, host='127.0.0.1', port=5000)
