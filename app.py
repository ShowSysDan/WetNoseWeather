from flask import Flask, render_template, jsonify, request, Response, send_file
import requests as req_lib
import json, os, time, logging, logging.handlers, socket, threading, tempfile, re, hashlib, math
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urlencode
import ipaddress, secrets

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('WETNOSE_SECRET_KEY') or secrets.token_hex(32)

# ── Paths ────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
ENV_FILE      = os.path.join(BASE_DIR, 'wetnose.env')

def _load_env_file(path):
    """Populate os.environ from a KEY=VALUE file. Existing env wins.
    Same syntax as systemd EnvironmentFile= so the file works for both
    `python app.py` and the gunicorn unit.
    """
    if not os.path.isfile(path):
        return
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass

_load_env_file(ENV_FILE)
# Re-read SECRET_KEY in case it was set by the env file.
app.config['SECRET_KEY'] = os.environ.get('WETNOSE_SECRET_KEY') or app.config['SECRET_KEY']

SETTINGS_FILE = os.path.join(BASE_DIR, 'settings.json')
LOG_DIR       = os.path.join(BASE_DIR, 'logs')
LOG_FILE      = os.path.join(LOG_DIR, 'alerts.jsonl')
NOTIFIED_FILE = os.path.join(LOG_DIR, 'notified.json')
WMS_CACHE_DIR = os.path.join(BASE_DIR, 'cache', 'wms')
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(WMS_CACHE_DIR, exist_ok=True)

HEADERS = {"User-Agent": "WetNoseWeather/1.0 (+https://github.com/showsysdan/wetnoseweather)"}

# ── Cross-worker / cross-thread coordination ─────────────
# Multiple gunicorn workers each load app.py; in-memory state like
# _last_alert_ids would diverge per worker. We persist dedup state to
# disk (notified.json) and serialize file writes with a lock.
_FILE_LOCK = threading.Lock()

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

# RFC 1123 hostname (also accepts IPv4 literal as a degenerate case)
_HOSTNAME_RE = re.compile(
    r'^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)'
    r'(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$'
)

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
    'show_satellite_ir':    False,
    'show_hurricane':       True,
    'nws_product':          'conus_bref_qcd',
    'wms_frame_count':      8,    # NWS/IEM animated frame count (2–24)
    'wms_frame_step_min':   5,    # minutes between frames (1–15)
    'anim_hold_last_ms':    0,    # pause on the final frame before looping (0–10000 ms)
    'sidebar_auto_hide':    True, # hide the alert sidebar when no active alerts
    'hard_reload_minutes':  60,   # how often the kiosk performs a defensive location.reload() (0 = never)
    # Notifications
    'webhook_url':            '',
    'webhook_min_severity':   'Severe',
    # Syslog
    'syslog_enabled':   False,
    'syslog_host':      '',
    'syslog_port':      514,
    'syslog_facility':  'local0',
    # Pin (point of interest with optional radius)
    'pin_enabled':         False,
    'pin_lat':             28.5383,
    'pin_lon':            -81.3792,
    'pin_label':           '',
    'pin_radius_enabled':  False,
    'pin_radius_miles':    10,
    # Storm cell tracks (IEM NEXRAD storm-attributes feed)
    'show_storm_cells':    False,
    'storm_cells_url':     'https://mesonet.agron.iastate.edu/geojson/nexrad_attr.geojson',
    'updated_at': 0,
}

# ── Settings helpers ─────────────────────────────────────
ALLOWED_RADAR_SOURCES    = {'rainviewer', 'nws', 'iem', 'iem_local'}
ALLOWED_NWS_PRODUCTS     = {'conus_bref_qcd','conus_cref_qcd','conus_bvel_qcd',
                             'conus_n1p_qcd','conus_ntp_qcd'}
ALLOWED_RING_STATIONS    = {'KMLB','KTBW'}
ALLOWED_SEVERITIES       = {'Extreme','Severe','Moderate','Minor'}
ALLOWED_SYSLOG_FACILITIES= {'local0','local1','local2','local3','local4',
                             'local5','local6','local7','user','daemon'}

def _is_safe_url(url):
    """Block SSRF: only http/https, no private/loopback/reserved targets.

    Also resolves hostnames so an attacker can't point a DNS name at
    127.0.0.1 / 10.0.0.0/8 / link-local / etc.
    """
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https') or not p.hostname:
            return False
        host = p.hostname
        candidates = []
        try:
            candidates.append(ipaddress.ip_address(host))
        except ValueError:
            try:
                for fam, _, _, _, sockaddr in socket.getaddrinfo(host, None):
                    if fam in (socket.AF_INET, socket.AF_INET6):
                        candidates.append(ipaddress.ip_address(sockaddr[0]))
            except socket.gaierror:
                return False
        if not candidates:
            return False
        for ip in candidates:
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_multicast or ip.is_reserved
                    or ip.is_unspecified):
                return False
        return True
    except Exception:
        return False

def _is_safe_syslog_host(host):
    """Reject empty/oversized values and obvious garbage. Allow IPs and DNS names."""
    if not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOSTNAME_RE.match(host))

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
    fi('pin_lat',-90,90); fi('pin_lon',-180,180)
    fi('pin_radius_miles', 0.1, 500)
    ii('map_zoom',2,18); ii('opacity',0,100); ii('anim_speed',50,5000)
    ii('rv_color',0,8); ii('syslog_port',1,65535)
    ii('wms_frame_count',2,24); ii('wms_frame_step_min',1,15)
    ii('anim_hold_last_ms',0,10000)
    ii('hard_reload_minutes',0,1440)
    en('radar_source',ALLOWED_RADAR_SOURCES); en('nws_product',ALLOWED_NWS_PRODUCTS)
    en('range_ring_station',ALLOWED_RING_STATIONS); en('webhook_min_severity',ALLOWED_SEVERITIES)
    en('syslog_facility',ALLOWED_SYSLOG_FACILITIES)
    for k in ('show_sidebar','show_alert_polygons','show_range_rings',
              'show_satellite_ir','show_hurricane','syslog_enabled',
              'pin_enabled','pin_radius_enabled','show_storm_cells',
              'sidebar_auto_hide'):
        if k in data: out[k]=bool(data[k])
    if 'storm_cells_url' in data:
        url = str(data['storm_cells_url']).strip()[:2048]
        if url and not _is_safe_url(url):
            raise ValueError('storm_cells_url must be http(s) and not target a private/loopback address')
        out['storm_cells_url'] = url
    if 'pin_label' in data:
        out['pin_label'] = str(data['pin_label']).strip()[:80]
    if 'webhook_url' in data:
        url=str(data['webhook_url']).strip()[:2048]
        if url and not _is_safe_url(url):
            raise ValueError('Webhook URL must use http/https and must not target a private/loopback address')
        out['webhook_url']=url
    if 'syslog_host' in data:
        host = str(data['syslog_host']).strip()[:253]
        if host and not _is_safe_syslog_host(host):
            raise ValueError('Syslog host must be a valid hostname or IP address')
        out['syslog_host'] = host
    return {**data,**out}


def _atomic_write_json(path, payload):
    """Write JSON atomically: write to a temp file in the same dir, then rename.

    Prevents corruption from a crash or concurrent writer truncating mid-write.
    """
    d = os.path.dirname(path) or '.'
    fd, tmp = tempfile.mkstemp(prefix='.tmp-', dir=d)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


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
    with _FILE_LOCK:
        current = load_settings()
        allowed = set(DEFAULT_SETTINGS.keys()) - {'updated_at'}
        current.update({k: v for k, v in data.items() if k in allowed})
        current['updated_at'] = time.time()
        _atomic_write_json(SETTINGS_FILE, current)
    setup_syslog(current)
    log_event('SETTINGS_CHANGED', {})
    # Warm the WMS cache for the new view so the next /output load is
    # served from disk. Fire-and-forget.
    try: warm_wms_cache_async()
    except NameError: pass  # warm not defined yet during early imports
    return current

# ── Syslog ───────────────────────────────────────────────
app_logger = logging.getLogger('wetnose')
app_logger.setLevel(logging.INFO)
app_logger.propagate = False  # keep our records from doubling into Flask's logger

def setup_syslog(settings=None):
    if settings is None:
        settings = load_settings()
    # Close and remove any existing SysLogHandler before reattaching,
    # otherwise repeated /api/settings POSTs would leak UDP sockets.
    for h in list(app_logger.handlers):
        if isinstance(h, logging.handlers.SysLogHandler):
            try: h.close()
            except Exception: pass
            app_logger.removeHandler(h)
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
        h.setFormatter(logging.Formatter('wetnose-weather: %(message)s'))
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
        with _FILE_LOCK, open(LOG_FILE, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception as e:  # noqa: BLE001
        app.logger.error(f'log_event write failed: {e}')
    sev  = props.get('severity', '')
    evt  = props.get('event', '')
    area = props.get('areaDesc', '')
    if event_type in ('NEW_ALERT', 'CLEARED_ALERT'):
        app_logger.info(f'{event_type} severity="{sev}" event="{evt}" area="{area}"')
    elif event_type == 'STATION_FAILOVER':
        app_logger.warning(f'STATION_FAILOVER station="{props.get("station","")}" reason="{evt}"')
    elif event_type == 'SERVER_START':
        app_logger.info('SERVER_START')
    elif event_type != 'SETTINGS_CHANGED':
        app_logger.info(event_type)

# ── Webhook dedup ────────────────────────────────────────
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
        _atomic_write_json(NOTIFIED_FILE, d)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f'save_notified failed: {e}')

def fire_webhook(settings, props):
    url = (settings.get('webhook_url') or '').strip()
    if not url or not _is_safe_url(url):
        return
    min_sev = SEV_ORDER.get(settings.get('webhook_min_severity', 'Severe'), 3)
    if SEV_ORDER.get(props.get('severity', ''), 0) < min_sev:
        return
    try:
        req_lib.post(url, json={
            'text':     f"{props.get('severity')} – {props.get('event')}",
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
# We persist dedup state to disk so multiple gunicorn workers don't each fire
# their own NEW_ALERT / webhook. notified.json is the source of truth for
# "have we already announced this alert ID?".
def process_alert_changes(features, settings):
    current = {
        f['properties']['id']: f['properties']
        for f in features
        if f.get('properties', {}).get('id')
    }
    with _FILE_LOCK:
        notified = load_notified()
        new_ids     = [aid for aid in current if aid not in notified]
        cleared_ids = [aid for aid in notified if aid not in current
                       and notified[aid] > time.time() - 24 * 3600]
        now = time.time()
        for aid in new_ids:
            notified[aid] = now
        if new_ids or cleared_ids:
            save_notified(notified)

    for aid in new_ids:
        log_event('NEW_ALERT', current[aid])
        fire_webhook(settings, current[aid])
    for aid in cleared_ids:
        log_event('CLEARED_ALERT', {'id': aid})

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

# ── API: geocode ──────────────────────────────────────────
# Nominatim (OpenStreetMap) free geocoder. Their usage policy requires a
# real User-Agent (we send one) and asks for no more than 1 req/sec from a
# single source. We cache results in-process for a day and pace outbound
# calls with a per-process lock to stay polite even if the kiosk admin
# spams the search button.
_GEOCODE_TTL_S      = 24 * 3600
_GEOCODE_MIN_GAP_S  = 1.1
_geocode_cache      = {}            # q.lower() -> (timestamp, result)
_geocode_lock       = threading.Lock()
_geocode_last_call  = [0.0]

@app.route('/api/geocode')
def api_geocode():
    q = (request.args.get('q', '') or '').strip()
    if not q:
        return jsonify({'error': 'Missing query'}), 400
    if len(q) > 200:
        return jsonify({'error': 'Query too long'}), 400
    key = q.lower()
    now = time.time()

    cached = _geocode_cache.get(key)
    if cached and now - cached[0] < _GEOCODE_TTL_S:
        return jsonify(cached[1])

    with _geocode_lock:
        gap = time.time() - _geocode_last_call[0]
        if gap < _GEOCODE_MIN_GAP_S:
            time.sleep(_GEOCODE_MIN_GAP_S - gap)
        _geocode_last_call[0] = time.time()

        try:
            resp = req_lib.get(
                'https://nominatim.openstreetmap.org/search',
                params={'q': q, 'format': 'json', 'limit': 1,
                        'countrycodes': 'us', 'addressdetails': 0},
                headers=HEADERS, timeout=8,
            )
            resp.raise_for_status()
            results = resp.json()
        except req_lib.RequestException as e:
            return jsonify({'error': f'Geocoder unavailable: {e}'}), 502

    if not results:
        return jsonify({'error': 'No results'}), 404
    r = results[0]
    try:
        out = {
            'lat':          round(float(r['lat']), 6),
            'lon':          round(float(r['lon']), 6),
            'display_name': str(r.get('display_name', ''))[:300],
        }
    except (KeyError, TypeError, ValueError):
        return jsonify({'error': 'Bad response from geocoder'}), 502
    _geocode_cache[key] = (now, out)
    # Bound the cache so a long-running process can't grow unbounded.
    if len(_geocode_cache) > 256:
        for old_key in list(_geocode_cache)[:64]:
            _geocode_cache.pop(old_key, None)
    return jsonify(out)

# ── API: WMS tile proxy + disk cache ─────────────────────
# Leaflet's L.tileLayer.wms points at this endpoint; we forward the
# query string to the upstream WMS, cache the response PNG on disk
# keyed by a hash of the canonical request, and serve from cache on
# subsequent hits. Big speed win on the kiosk: page refreshes,
# frame-loop ticks, and source switches become local disk reads.

WMS_SOURCES = {
    'nws':       'https://opengeo.ncep.noaa.gov/geoserver/conus/ows',
    'iem':       'https://mesonet.agron.iastate.edu/cgi-bin/wms/nexrad/n0q-t.cgi',
    'iem_local': 'https://mesonet.agron.iastate.edu/cgi-bin/wms/nexrad/ridge.cgi',
}
# Layers the proxy will forward. Acts as a whitelist so the endpoint
# can't be turned into an open WMS proxy.
WMS_ALLOWED_LAYERS = ALLOWED_NWS_PRODUCTS | {'nexrad-n0q-wmst', 'single'}
# Allowed RIDGE prod (product code) and sector (radar site, sans K
# prefix) values. Constrained so /api/wms_tile/iem_local can't be
# pointed at arbitrary stations or products.
WMS_RIDGE_PRODS    = {'N0B', 'N0Q', 'N0R'}
WMS_RIDGE_SECTORS  = {'MLB', 'TBW'}

try:
    WMS_CACHE_MAX_BYTES = int(os.environ.get('WETNOSE_WMS_CACHE_MB', '2048')) * 1024 * 1024
except ValueError:
    WMS_CACHE_MAX_BYTES = 2048 * 1024 * 1024

_wms_cache_lock     = threading.Lock()
_wms_cache_last_gc  = [0.0]
_WMS_CACHE_GC_EVERY = 300  # seconds between sweeps

def _wms_cache_key(source, params):
    # Canonicalize for stability across Leaflet request orderings.
    # `sector` and `prod` carry the per-station RIDGE selection. `_t`
    # is a client-supplied minute bucket used by the per-station feed
    # (which serves a "latest" image with no TIME dimension) to make
    # the cache key rotate every minute.
    relevant = ['layers','styles','format','transparent','time',
                'bbox','width','height','srs','crs','version',
                'sector','prod','_t']
    canonical = '|'.join(f'{k}={params.get(k,"")}' for k in relevant)
    h = hashlib.sha256(f'{source}|{canonical}'.encode()).hexdigest()
    return h

def _wms_cache_sweep():
    """Walk the cache, sort by mtime, evict oldest until under budget."""
    try:
        entries = []
        total = 0
        for name in os.listdir(WMS_CACHE_DIR):
            path = os.path.join(WMS_CACHE_DIR, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, path))
            total += st.st_size
        if total <= WMS_CACHE_MAX_BYTES:
            return
        entries.sort()  # oldest first
        for _, size, path in entries:
            if total <= WMS_CACHE_MAX_BYTES:
                break
            try:
                os.unlink(path)
                total -= size
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f'wms cache sweep failed: {e}')

def _wms_cache_maybe_sweep():
    now = time.time()
    if now - _wms_cache_last_gc[0] < _WMS_CACHE_GC_EVERY:
        return
    _wms_cache_last_gc[0] = now
    threading.Thread(target=_wms_cache_sweep, daemon=True).start()

def _fetch_wms_to_cache(source, params, raw_query=None):
    """Fetch a WMS tile from upstream and write into the on-disk cache.
    Returns (cache_path, content_bytes, content_type) on success, or
    (None, None, None) on failure. Safe to call from any thread.
    """
    if source not in WMS_SOURCES:
        return (None, None, None)
    layers = params.get('layers', '')
    if layers not in WMS_ALLOWED_LAYERS:
        return (None, None, None)
    if params.get('request', 'GetMap') != 'GetMap':
        return (None, None, None)
    if source == 'iem_local':
        if params.get('prod', '')   not in WMS_RIDGE_PRODS:   return (None, None, None)
        if params.get('sector', '') not in WMS_RIDGE_SECTORS: return (None, None, None)

    key        = _wms_cache_key(source, params)
    cache_path = os.path.join(WMS_CACHE_DIR, key + '.png')
    if os.path.exists(cache_path):
        try: os.utime(cache_path, None)
        except OSError: pass
        try:
            with open(cache_path, 'rb') as f:
                return (cache_path, f.read(), 'image/png')
        except OSError:
            pass

    query = raw_query if raw_query is not None else urlencode(params)
    upstream = WMS_SOURCES[source] + '?' + query
    try:
        resp = req_lib.get(upstream, headers=HEADERS, timeout=15)
    except req_lib.RequestException as e:
        app_logger.warning(f'wms upstream fetch failed ({source}): {e}')
        return (None, None, None)

    ctype = resp.headers.get('Content-Type', '')
    if resp.status_code != 200 or not ctype.startswith('image/'):
        return (None, None, ctype)

    fd, tmp_path = tempfile.mkstemp(dir=WMS_CACHE_DIR, prefix='.tmp-', suffix='.png')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(resp.content)
        os.replace(tmp_path, cache_path)
    except OSError as e:
        try: os.unlink(tmp_path)
        except OSError: pass
        app.logger.warning(f'wms cache write failed: {e}')
        cache_path = None
    return (cache_path, resp.content, ctype)


@app.route('/api/wms_tile/<source>')
def api_wms_tile(source):
    if source not in WMS_SOURCES:
        return jsonify({'error': 'Unknown source'}), 404
    params = {k.lower(): v for k, v in request.args.items()}
    layers = params.get('layers', '')
    if layers not in WMS_ALLOWED_LAYERS:
        return jsonify({'error': f'Layer not allowed: {layers}'}), 400
    if params.get('request', 'GetMap') != 'GetMap':
        return jsonify({'error': 'Only GetMap is proxied'}), 400
    if source == 'iem_local':
        if params.get('prod', '')   not in WMS_RIDGE_PRODS:
            return jsonify({'error': 'Bad RIDGE prod'}), 400
        if params.get('sector', '') not in WMS_RIDGE_SECTORS:
            return jsonify({'error': 'Bad RIDGE sector'}), 400

    cache_path, content, ctype = _fetch_wms_to_cache(
        source, params, raw_query=urlencode(request.args))
    _wms_cache_maybe_sweep()

    if content is None:
        return jsonify({'error': 'Upstream WMS fetch failed',
                        'content_type': ctype}), 502

    # Cache key includes TIME, so tiles never go stale — let the browser
    # cache aggressively. A day is plenty for a kiosk that hits the same
    # frame-set repeatedly.
    if cache_path and os.path.exists(cache_path):
        return send_file(cache_path, mimetype='image/png',
                         max_age=86400, conditional=True)
    out = Response(content, mimetype=ctype or 'image/png')
    out.headers['Cache-Control'] = 'public, max-age=86400'
    return out

# ── WMS cache pre-warm ───────────────────────────────────
# Before the browser asks, fetch the visible tile pyramid for the
# currently configured map view across all animated frames. Triggered
# on service start, after every settings save, and every minute (the
# step-boundary check kicks new frames into the warm path as they
# come online). All requests go through _fetch_wms_to_cache which
# uses the same cache keys the proxy uses, so the kiosk's WMS layer
# is fed straight off disk on first paint.

# Viewport buffer in 256px tiles around the configured center. 1920×1080
# is roughly 7.5×4.25 tiles; we round up and add a 1-tile margin on
# each side so a small pan after first load is also warm.
_WARM_TILES_X = 10
_WARM_TILES_Y = 6
_warm_lock    = threading.Lock()
_warm_thread  = [None]

def _tile_xy_for_lonlat(lon, lat, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y

def _tile_bbox_3857(z, x, y):
    half = 20037508.34278925
    n = 2 ** z
    span = 2 * half / n
    minx = -half + x * span
    maxx = -half + (x + 1) * span
    maxy =  half - y * span
    miny =  half - (y + 1) * span
    return (minx, miny, maxx, maxy)

def _warm_params(layer, frame_iso, bbox):
    # These keys must match what Leaflet's L.tileLayer.wms 1.1.1 emits
    # (lower-cased), so the cache key collides with what the browser
    # will request later.
    return {
        'service':     'WMS',
        'request':     'GetMap',
        'version':     '1.1.1',
        'layers':      layer,
        'styles':      '',
        'format':      'image/png',
        'transparent': 'true',
        'srs':         'EPSG:3857',
        'bbox':        f'{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}',
        'width':       '256',
        'height':      '256',
        'time':        frame_iso,
    }

def _build_warm_frames(count, step_min):
    step_s = step_min * 60
    latest = int(time.time() // step_s) * step_s
    out = []
    for i in range(count - 1, -1, -1):
        t = latest - i * step_s
        out.append(datetime.fromtimestamp(t, tz=timezone.utc)
                   .strftime('%Y-%m-%dT%H:%M:%S.000Z'))
    return out

def _do_warm():
    try:
        s = load_settings()
        source = s.get('radar_source')
        if source not in ('nws', 'iem'):
            return
        layer = 'nexrad-n0q-wmst' if source == 'iem' else \
                s.get('nws_product', 'conus_bref_qcd')
        lat   = float(s.get('map_lat',  28.5383))
        lon   = float(s.get('map_lon', -81.3792))
        zoom  = int  (s.get('map_zoom', 8))
        count = int  (s.get('wms_frame_count',    8))
        step  = int  (s.get('wms_frame_step_min', 5))

        cx, cy = _tile_xy_for_lonlat(lon, lat, zoom)
        tx0    = int(cx - _WARM_TILES_X / 2)
        ty0    = int(cy - _WARM_TILES_Y / 2)

        frames = _build_warm_frames(count, step)
        tasks  = []
        for iso in frames:
            for dx in range(_WARM_TILES_X):
                for dy in range(_WARM_TILES_Y):
                    tx, ty = tx0 + dx, ty0 + dy
                    if tx < 0 or ty < 0 or tx >= 2**zoom or ty >= 2**zoom:
                        continue
                    bbox = _tile_bbox_3857(zoom, tx, ty)
                    tasks.append(_warm_params(layer, iso, bbox))

        # 8 parallel upstream fetches. IEM and NCEP both tolerate that
        # comfortably from a single client.
        ok = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = [ex.submit(_fetch_wms_to_cache, source, p) for p in tasks]
            for f in as_completed(futures):
                path, content, _ = f.result()
                if content is not None:
                    ok += 1
        app_logger.info(
            f'WMS warm: {ok}/{len(tasks)} tiles ready (source={source}, '
            f'frames={count}, zoom={zoom})')
        _wms_cache_maybe_sweep()
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f'wms warm failed: {e}')

def warm_wms_cache_async():
    """Kick a warm, dropping the request if one is already in flight."""
    with _warm_lock:
        prev = _warm_thread[0]
        if prev is not None and prev.is_alive():
            return False
        t = threading.Thread(target=_do_warm, daemon=True, name='wms-warm')
        _warm_thread[0] = t
        t.start()
    return True

def _warm_loop():
    """Periodically rebuild the warm cache as new step boundaries arrive."""
    while True:
        try:
            time.sleep(60)
            warm_wms_cache_async()
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f'warm loop iteration failed: {e}')

threading.Thread(target=_warm_loop, daemon=True, name='wms-warm-loop').start()

# ── API: storm cells ──────────────────────────────────────
# Proxy + cache for the IEM (Iowa Environmental Mesonet) NEXRAD
# storm-attributes GeoJSON. URL is settable in /settings so it can be
# pointed at a different feed without redeploying. We cache for 30s
# per URL to be a polite client and to absorb the kiosk's polling.
_STORM_CACHE_TTL_S  = 30
_storm_cache        = {}    # url -> (timestamp, payload_dict, status_int)
_storm_cache_lock   = threading.Lock()

@app.route('/api/storm_cells')
def api_storm_cells():
    s   = load_settings()
    url = (s.get('storm_cells_url') or '').strip()
    if not url:
        return jsonify({'type': 'FeatureCollection', 'features': [],
                        'error': 'storm_cells_url is not set'}), 200
    if not _is_safe_url(url):
        return jsonify({'error': 'Configured storm_cells_url is not safe'}), 400

    now = time.time()
    with _storm_cache_lock:
        hit = _storm_cache.get(url)
        if hit and now - hit[0] < _STORM_CACHE_TTL_S:
            return jsonify(hit[1]), hit[2]

    try:
        resp = req_lib.get(url, headers=HEADERS, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or data.get('type') != 'FeatureCollection':
            return jsonify({'type': 'FeatureCollection', 'features': [],
                            'error': 'Upstream did not return a FeatureCollection'}), 502
        with _storm_cache_lock:
            _storm_cache[url] = (now, data, 200)
            # Bound the cache: only ever a handful of URLs.
            if len(_storm_cache) > 8:
                _storm_cache.pop(next(iter(_storm_cache)), None)
        return jsonify(data), 200
    except req_lib.RequestException as e:
        app_logger.warning(f'storm_cells fetch failed: {e}')
        # Serve stale data if we have any so the kiosk doesn't go blank
        # because IEM hiccuped for a single poll.
        with _storm_cache_lock:
            hit = _storm_cache.get(url)
        if hit:
            return jsonify({**hit[1], 'stale': True}), 200
        return jsonify({'type': 'FeatureCollection', 'features': [],
                        'error': str(e)}), 502
    except ValueError as e:
        return jsonify({'type': 'FeatureCollection', 'features': [],
                        'error': f'Upstream returned non-JSON: {e}'}), 502

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
    try:
        n = min(max(int(request.args.get('n', 200)), 1), 500)
    except (TypeError, ValueError):
        n = 200
    entries = []
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE) as f:
                lines = f.readlines()
            entries = [json.loads(l) for l in lines[-n:] if l.strip()]
            entries.reverse()
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f'get_logs read failed: {e}')
    return jsonify({'entries': entries})

@app.route('/api/logs/clear', methods=['POST'])
def clear_logs():
    try:
        with _FILE_LOCK, open(LOG_FILE, 'w') as f:
            f.truncate(0)
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

# Cross-worker failover dedup: write the active station to a tiny file so we
# only log STATION_FAILOVER once per actual transition, not once per worker.
_STATION_STATE_FILE = os.path.join(LOG_DIR, 'active_station.txt')

def _read_active_station():
    try:
        with open(_STATION_STATE_FILE) as f:
            v = f.read().strip()
            return v or RADAR_PRIMARY
    except FileNotFoundError:
        return RADAR_PRIMARY
    except Exception:
        return RADAR_PRIMARY

def _write_active_station(sid):
    try:
        d = os.path.dirname(_STATION_STATE_FILE) or '.'
        fd, tmp = tempfile.mkstemp(prefix='.tmp-', dir=d)
        with os.fdopen(fd, 'w') as f:
            f.write(sid)
        os.replace(tmp, _STATION_STATE_FILE)
    except Exception as e:
        app.logger.warning(f'Could not persist active station: {e}')

@app.route('/api/radar_status')
def radar_status():
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
    with _FILE_LOCK:
        prev = _read_active_station()
        if active != prev:
            _write_active_station(active)
            log_event('STATION_FAILOVER', {'station': active,
                      'event': f'Switched from {prev} to {active}'})

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
log_event('SERVER_START', {'event': 'Wet Nose Weather started'})
warm_wms_cache_async()

if __name__ == '__main__':
    debug = os.environ.get('WETNOSE_DEBUG', '').lower() in ('1', 'true', 'yes')
    host = os.environ.get('WETNOSE_HOST', '127.0.0.1')
    try:
        port = int(os.environ.get('WETNOSE_PORT', '5100'))
        if not (1 <= port <= 65535):
            raise ValueError
    except ValueError:
        raise SystemExit(f'WETNOSE_PORT must be an integer 1-65535, got: {os.environ.get("WETNOSE_PORT")!r}')
    app.run(debug=debug, host=host, port=port)
