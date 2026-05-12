# NEXRAD Weather Radar Display

A Flask-based NEXRAD radar display system built for professional AV/IT environments. Designed to run as a locked 1920×1080 kiosk display with a separate settings interface for full remote control — no interaction needed on the output screen.

Built around Orlando/Central Florida (KMLB primary, KTBW fallback) but fully configurable for any US location.

---

## Features

### Radar Sources
- **RainViewer** (animated loop) — 12+ past frames plus nowcast, color scheme selectable (8 palettes), animation speed adjustable, smart per-minute frame-check (only reloads tiles when a new timestamp appears)
- **NWS WMS** (live static) — NOAA/NWS CONUS composite tile service, product-selectable (Base Reflectivity, Composite Reflectivity, Base Velocity, 1-hr Precipitation, Storm-Total Precipitation), poll interval driven by current VCP scan mode

### Radar Station Monitoring
- Dual-station failover: **KMLB Melbourne** (primary) → **KTBW Tampa Bay** (fallback)
- Staleness detection via `rda.timestamp` — station marked offline if data is older than 15 minutes, regardless of reported operability status
- VCP (Volume Coverage Pattern) detection maps scan mode to appropriate poll interval:

  | VCP | Mode | Redraw Interval |
  |-----|------|----------------|
  | 12, 212 | Precipitation (severe) | ~4 min |
  | 112 | Precipitation | ~4.5 min |
  | 35, 215 | Precipitation | ~6 min |
  | 31, 32 | Clear Air | ~10 min |

- Station failover events are logged and syslogged

### Overlay Layers
- **Alert Polygons** — NWS warned areas drawn as GeoJSON fills on the map, color-coded by severity (Extreme/Severe/Moderate/Minor)
- **Range Rings** — dashed distance rings at 50, 100, 200 nm from the selected radar site (KMLB or KTBW)
- **Satellite IR** — RainViewer infrared satellite overlay (latest available frame, free API tier)
- **Hurricane Track** — NOAA NHC/nowCOAST WMS cone of uncertainty and track forecast; passive/invisible when no active storms

### NWS Weather Alerts
- Active alerts fetched every 5 minutes for the configured location
- Severity-colored banner across the top of every view
- Sidebar panel shows up to 7 active alert cards (severity badge, event name, headline, expiry)
- Alert polygons overlay the map automatically
- New/cleared alert events written to the event log

### Output Display (`/output`)
- Fixed 1920×1080, `cursor: none`, zero user interaction (dragging, zooming, clicking all disabled)
- Map position and zoom set entirely from `/settings`
- Polls for settings changes every 10 seconds — reloads automatically when anything changes
- Health-checks Flask every 5 seconds — fades to black if server dies, reloads when it comes back
- Hard reload every 15 minutes (configurable via `HARD_RELOAD_MS` constant)
- Countdown timer visible in sidebar footer
- NWS tile error indicator (appears when ≥3 consecutive tile failures detected)

### Settings Interface (`/settings`)
- **Map Position** — interactive mini-map with draggable marker, click-to-move, on-screen arrow buttons, arrow key controls (Shift = 5× step), +/− zoom, synced lat/lon/zoom text fields
- Radar source, color scheme, animation speed, NWS product selector
- Per-layer toggles: alert polygons, range rings (with station selector), satellite IR, hurricane track
- Radar opacity slider
- Alert sidebar toggle
- Webhook notifications (URL + minimum severity threshold)
- Syslog configuration (host, port, UDP facility)
- Live radar station status panel showing VCP, operability, data age for both KMLB and KTBW
- Alert log viewer (last 200 events, color-coded, with clear button)

### Notifications & Logging
- **Webhook** — HTTP POST on new alerts meeting the minimum severity threshold; payload is JSON-compatible with Slack, Teams, and generic endpoints. Alert IDs persisted to `logs/notified.json` (48-hour window) to prevent duplicate fires across server restarts
- **Syslog** — UDP syslog via Python `SysLogHandler`; configurable host, port, facility. Events: `SERVER_START`, `NEW_ALERT`, `CLEARED_ALERT`, `STATION_FAILOVER`, `SETTINGS_CHANGED`
- **Alert log** — JSONL file at `logs/alerts.jsonl` capturing all events with ISO timestamps, viewable and clearable from the settings page

### Security
- Input validation on all settings fields (type coercion, range checks, enum allowlists)
- SSRF protection on webhook URL (blocks private/loopback IPs, enforces `http`/`https` only)
- Secret key via `NEXRAD_SECRET_KEY` environment variable (auto-generated if absent, but set explicitly in production)
- Debug mode disabled unless `NEXRAD_DEBUG=1` is set
- Server binds to `127.0.0.1` only — put nginx/Caddy in front for LAN/WAN access
- Systemd unit includes `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem`, `ProtectHome`
- `settings.json` and `logs/` are in `.gitignore` — webhook URLs and syslog addresses never committed

---

## Requirements

- Python 3.10+
- Debian 12 / Ubuntu 22.04+ (or any systemd Linux)
- `curl` (for watchdog script)
- Network access to `api.weather.gov`, `api.rainviewer.com`, `tilecache.rainviewer.com`, `opengeo.ncep.noaa.gov`, `nowcoast.noaa.gov`

---

## Installation

### Development / quick start

```bash
git clone https://github.com/YOUR_USERNAME/nexrad-radar.git
cd nexrad-radar
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp settings.example.json settings.json   # edit as needed, or use /settings
python app.py
```

Open `http://localhost:5000/settings` to configure, then `http://localhost:5000/output` for the display.

### Production (Debian/Ubuntu + systemd + gunicorn)

**1. Create app user and directory**
```bash
sudo useradd -r -s /sbin/nologin -d /opt/nexrad nexrad
sudo mkdir -p /opt/nexrad/logs
sudo git clone https://github.com/YOUR_USERNAME/nexrad-radar.git /opt/nexrad
sudo chown -R nexrad:nexrad /opt/nexrad
```

**2. Install dependencies into a venv**
```bash
sudo -u nexrad python3 -m venv /opt/nexrad/venv
sudo -u nexrad /opt/nexrad/venv/bin/pip install -r /opt/nexrad/requirements.txt
```

**3. Create settings file**
```bash
sudo -u nexrad cp /opt/nexrad/settings.example.json /opt/nexrad/settings.json
```

**4. Generate a secret key**
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

**5. Install and configure the systemd service**
```bash
sudo cp /opt/nexrad/nexrad.service /etc/systemd/system/
# Edit the service file to set NEXRAD_SECRET_KEY to the value from step 4
sudo nano /etc/systemd/system/nexrad.service
sudo systemctl daemon-reload
sudo systemctl enable nexrad
sudo systemctl start nexrad
sudo systemctl status nexrad
```

**6. Set up nginx reverse proxy** (recommended)
```nginx
server {
    listen 80;
    server_name radar.yourdomain.local;

    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 30s;
    }
}
```

**7. Install the watchdog cron job**
```bash
sudo crontab -e
# Add:
*/5 * * * * /opt/nexrad/watchdog.sh
```

### Updates

```bash
cd /opt/nexrad
sudo -u nexrad git pull
sudo -u nexrad /opt/nexrad/venv/bin/pip install -r requirements.txt
sudo systemctl restart nexrad
```

---

## Configuration Reference

All settings are managed through `/settings` and stored in `settings.json`. The file is excluded from git — use `settings.example.json` as a template.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `radar_source` | string | `"rainviewer"` | `"rainviewer"` or `"nws"` |
| `rv_color` | int | `6` | RainViewer color scheme (0–7) |
| `opacity` | int | `70` | Radar layer opacity (0–100) |
| `anim_speed` | int | `500` | RainViewer frame interval in ms |
| `show_sidebar` | bool | `true` | Show alert sidebar on output display |
| `map_lat` | float | `28.5383` | Map center latitude |
| `map_lon` | float | `-81.3792` | Map center longitude |
| `map_zoom` | int | `8` | Leaflet zoom level (2–18) |
| `show_alert_polygons` | bool | `true` | Draw NWS warning polygons on map |
| `show_range_rings` | bool | `false` | Draw 50/100/200nm rings from radar site |
| `range_ring_station` | string | `"KMLB"` | `"KMLB"` or `"KTBW"` |
| `show_satellite_ir` | bool | `false` | RainViewer IR satellite overlay |
| `show_hurricane` | bool | `true` | NHC/nowCOAST tropical cyclone WMS |
| `nws_product` | string | `"conus_bref_qcd"` | NWS WMS product layer |
| `webhook_url` | string | `""` | HTTP/HTTPS endpoint for alert POSTs |
| `webhook_min_severity` | string | `"Severe"` | `"Extreme"`, `"Severe"`, `"Moderate"`, `"Minor"` |
| `syslog_enabled` | bool | `false` | Enable UDP syslog output |
| `syslog_host` | string | `""` | Syslog server hostname or IP |
| `syslog_port` | int | `514` | Syslog UDP port |
| `syslog_facility` | string | `"local0"` | Syslog facility |

### NWS WMS Product Options

| Value | Description |
|-------|-------------|
| `conus_bref_qcd` | Base Reflectivity (default) |
| `conus_cref_qcd` | Composite Reflectivity |
| `conus_bvel_qcd` | Base Velocity |
| `conus_n1p_qcd` | 1-Hour Precipitation |
| `conus_ntp_qcd` | Storm-Total Precipitation |

### RainViewer Color Schemes

| Value | Name |
|-------|------|
| 0 | Original |
| 1 | Universal Blue |
| 2 | TITAN |
| 3 | The Weather Channel |
| 4 | Meteored |
| 5 | NEXRAD Level III |
| 6 | Rainbow / SELEX-SI (default) |
| 7 | Dark Sky |

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Interactive radar view |
| `GET` | `/output` | Kiosk display (1920×1080, locked) |
| `GET` | `/settings` | Settings interface |
| `GET` | `/api/health` | Server health ping — `{"status":"ok"}` |
| `GET` | `/api/settings` | Current settings as JSON |
| `POST` | `/api/settings` | Save settings (validated); triggers output reload |
| `GET` | `/api/alerts` | NWS active alerts for configured location |
| `GET` | `/api/alert_polygons` | GeoJSON polygon features from active alerts |
| `GET` | `/api/rainviewer` | Proxied RainViewer weather-maps manifest |
| `GET` | `/api/hurricane` | NHC active Atlantic/East Pacific storm list |
| `GET` | `/api/radar_status` | VCP, operability, age, failover state for KMLB+KTBW |
| `GET` | `/api/station_coords` | Lat/lon of KMLB and KTBW radar sites |
| `GET` | `/api/logs?n=200` | Last N alert log entries (max 500) |
| `POST` | `/api/logs/clear` | Clear the alert log file |

### Webhook Payload

When a new alert meets the minimum severity threshold, a POST is sent to `webhook_url`:

```json
{
  "text": "⚠️ Severe – Tornado Warning",
  "event": "Tornado Warning",
  "severity": "Extreme",
  "headline": "Tornado Warning issued for Orange County FL until 9:45 PM EDT",
  "area": "Orange County FL",
  "expires": "2024-08-15T21:45:00-04:00"
}
```

The `text` field is compatible with Slack and Teams incoming webhook format. For other systems, use the individual fields.

---

## External Data Sources

| Source | Used For | Update Frequency |
|--------|----------|-----------------|
| `api.rainviewer.com` | Radar frame manifest, satellite IR | ~6–10 min |
| `tilecache.rainviewer.com` | Radar and satellite tiles | Per frame |
| `api.weather.gov` | Active alerts, alert polygons | Fetched every 5 min |
| `api.weather.gov/radar/stations/` | VCP, station operability, RDA timestamp | Fetched every 5 min |
| `opengeo.ncep.noaa.gov/geoserver/conus` | NWS CONUS radar WMS tiles | Live (VCP-driven redraw) |
| `nowcoast.noaa.gov` | Hurricane track/cone WMS | Updated every 10 min by NHC |
| `www.nhc.noaa.gov` | Active storm list | Fetched on page load |
| `basemaps.cartocdn.com` | Dark basemap tiles | Static CDN |

All external requests use a descriptive `User-Agent` header as required by `api.weather.gov`.

---

## File Structure

```
nexrad-radar/
├── app.py                  # Flask application, API routes, alert logic
├── requirements.txt        # Python dependencies (pinned)
├── settings.example.json   # Template — copy to settings.json
├── nexrad.service          # Systemd unit (production deployment)
├── watchdog.sh             # Health-check + restart script (cron)
├── .gitignore
├── README.md
├── templates/
│   ├── output.html         # 1920×1080 kiosk display (no interaction)
│   ├── settings.html       # Settings admin interface
│   └── index.html          # Interactive radar view (development/monitoring)
└── logs/                   # Created at runtime, excluded from git
    ├── alerts.jsonl         # Event log (new/cleared alerts, server events)
    └── notified.json        # Persisted webhook dedup state
```

---

## Changing Location

The map center and alert area are both controlled by `map_lat`/`map_lon` in settings — change them via the mini-map on `/settings` and save. The NWS alerts API uses the same coordinates.

For locations outside Central Florida you should also update the radar station fallback constants in `app.py`:

```python
RADAR_PRIMARY   = 'KMLB'   # Replace with your nearest WSR-88D site
RADAR_SECONDARY = 'KTBW'   # Replace with your secondary site

STATION_COORDS = {
    'KMLB': {'lat': 28.1128, 'lon': -80.6547, 'name': 'Melbourne FL'},
    'KTBW': {'lat': 27.7056, 'lon': -82.4019, 'name': 'Tampa Bay FL'},
    # Add your stations here
}
```

A full list of WSR-88D site IDs and coordinates is available at [https://www.roc.noaa.gov/WSR88D/Maps.aspx](https://www.roc.noaa.gov/WSR88D/Maps.aspx).

---

## Known Limitations

- **RainViewer lightning** — lightning strike tiles require a paid RainViewer API key; the free public API serves radar and satellite IR only. The satellite IR layer is provided as the free-tier alternative.
- **NWS WMS availability** — `opengeo.ncep.noaa.gov` is a government service and experiences occasional outages, especially during heavy weather events. The NWS tile health indicator on the output display will show when tiles are failing.
- **Hurricane track** — the nowCOAST WMS renders nothing outside active storm seasons; no visual change to the output when enabled and no storms are active.
- **Alert polygons** — not all NWS alerts include polygon geometry (some use zone-based areas instead). Cards will still appear in the sidebar for zone-only alerts even if no polygon is drawn.
- **Kiosk display refresh** — the 15-minute hard reload is a browser-level `location.reload()`. For true kiosk deployments, consider also setting the browser's own session restore and crash-recovery options to point back to `/output`.

---

## Development Notes

- **Debug mode** — set `NEXRAD_DEBUG=1` in the environment before running `python app.py` to enable Flask's reloader and debugger. Never enable in production.
- **Running tests** — there are no automated tests currently. Contributions welcome.
- **CDN dependencies** — Leaflet (1.9.4) and Barlow Condensed/Share Tech Mono fonts are loaded from CDNs. For air-gapped or fully offline deployments, download and serve these locally.

---

## License

MIT — see `LICENSE` for details.

---

## Contributing

Issues and pull requests welcome. When reporting a bug, please include your OS/Python version, the contents of `logs/alerts.jsonl` (if relevant), and the Flask error log output.
