# Wet Nose Weather

A Flask-based NEXRAD weather radar display built for kiosk / AV-IT environments. Runs as a locked 1920×1080 output screen with a separate web settings interface for full remote control — no interaction needed on the display itself.

Default geography is Central Florida (KMLB primary, KTBW fallback) but everything is configurable for any US location.

---

## Features

### Radar Sources
- **RainViewer** (animated loop) — 12+ past frames plus nowcast, 8 selectable color palettes, adjustable animation speed, per-minute manifest poll (only reloads tiles when a new timestamp appears).
- **NWS WMS** (animated) — NOAA/NWS CONUS composite tile service. Selectable products: Base Reflectivity, Composite Reflectivity, Base Velocity, 1-hour Precipitation, Storm-Total Precipitation. Renders the last 8 frames at 5-minute intervals using the WMS TIME dimension, animated at the configured speed; the frame-set advances forward as new step boundaries are reached.
- **IEM Super-Res (N0Q)** (animated) — Iowa Environmental Mesonet's CONUS-wide super-resolution base reflectivity mosaic, sampled at 0.5° azimuth × 250 m range (the same resolution RadarScope shows when you zoom in on base reflectivity). Same WMS TIME-frame animation as the NWS source. Defaults to layer `nexrad-n0q-wmst` at `https://mesonet.agron.iastate.edu/cgi-bin/wms/nexrad/n0q.cgi`.

### Radar Station Monitoring
- Dual-station failover: **KMLB Melbourne** (primary) → **KTBW Tampa Bay** (fallback).
- Staleness detection via `rda.timestamp` — a station is marked offline if its data is older than 15 minutes, regardless of reported operability.
- VCP (Volume Coverage Pattern) → poll interval:

  | VCP | Mode | Redraw |
  |-----|------|--------|
  | 12, 212 | Precipitation (severe) | ~4 min |
  | 112 | Precipitation | ~4.5 min |
  | 35, 215 | Precipitation | ~6 min |
  | 31, 32 | Clear Air | ~10 min |

- Failover transitions are logged once (cross-worker deduped via on-disk state) and syslogged.

### Overlay Layers
- **Alert Polygons** — NWS warned areas drawn as GeoJSON fills, color-coded by severity (Extreme/Severe/Moderate/Minor).
- **Range Rings** — dashed distance rings at 50/100/200 nm from the selected radar site.
- **Satellite IR** — RainViewer infrared satellite overlay (latest available frame, free API tier).
- **Hurricane Track** — NHC / nowCOAST cone-of-uncertainty WMS; invisible when no active storms.
- **Storm Cell Tracks** — NEXRAD storm-attribute cells from the Iowa Environmental Mesonet (IEM) feed, rendered as markers with a label (cell ID + max dBZ) and a dashed ~30-minute projected track line based on the cell's reported motion. Pulled by the server, cached 30 s, served stale on transient IEM hiccups. URL is overridable in `/settings`.

### NWS Alerts
- Active alerts fetched every 5 minutes for the configured location.
- Severity-colored banner along the top of every view.
- Sidebar panel shows the active alert cards (severity badge, event, headline, expiry).
- New/cleared alert events are written to the JSONL event log.

### Output Display (`/output`)
- Fixed 1920×1080, `cursor: none`, zero user interaction (drag/zoom/click disabled).
- Map position and zoom are set entirely from `/settings`.
- Polls for settings changes every 10 s and reloads automatically when anything changes.
- Health-checks Flask every 5 s — fades to black if the server dies, reloads when it comes back.
- Hard reload every 15 minutes (configurable via the `HARD_RELOAD_MS` constant).
- NWS tile-error indicator (appears after ≥3 consecutive tile failures).

### Settings Interface (`/settings`)
- Interactive mini-map with draggable marker, click-to-move, arrow buttons, arrow-key controls (Shift = 5× step), +/− zoom, synced lat/lon/zoom text fields.
- Radar source, color scheme, animation speed, NWS product selector.
- Per-layer toggles: alert polygons, range rings (with station selector), satellite IR, hurricane track.
- Radar opacity slider, alert-sidebar toggle.
- Webhook notifications (URL + minimum severity threshold).
- Syslog configuration (host, port, UDP facility).
- Live radar-station status panel (VCP, operability, data age for KMLB and KTBW).
- Alert log viewer (last 200 events, color-coded, clearable).
- **Pin / point of interest** — drop a labelled marker (e.g. "Home") on the output map. Place it by typing a US address (geocoded server-side via Nominatim with a 24-hour cache and 1 req/sec rate limit), dragging the pin on the mini-map, or typing lat/lon directly. Optional dashed radius circle around the pin in miles.

### Notifications & Logging
- **Webhook** — HTTP POST on new alerts meeting the minimum severity threshold; JSON payload compatible with Slack/Teams/generic endpoints. Alert IDs are persisted to `logs/notified.json` (48-hour window) so a server restart doesn't re-fire alerts and gunicorn workers can't double-fire.
- **Syslog** — UDP syslog via Python's `SysLogHandler`; configurable host, port, facility. Emits: `SERVER_START`, `NEW_ALERT`, `CLEARED_ALERT`, `STATION_FAILOVER`, `SETTINGS_CHANGED`.
- **Alert log** — JSONL file at `logs/alerts.jsonl` with ISO timestamps, viewable and clearable from the settings page.

### Security
- Input validation on every settings field (type coercion, range checks, enum allowlists).
- SSRF protection on webhook URL — `http`/`https` only, and the **resolved IPs** (not just literals) are rejected if private/loopback/link-local/multicast/reserved/unspecified.
- Syslog host validated against an RFC 1123 hostname pattern or a literal IP.
- Settings and notification-state files are written **atomically** (temp file + `os.replace`) so a crash mid-write can't corrupt JSON.
- `SysLogHandler` instances are properly closed and removed before re-attaching, preventing UDP socket leaks across config changes.
- Secret key via `WETNOSE_SECRET_KEY` env var (auto-generated if absent; set explicitly in production).
- Debug mode off unless `WETNOSE_DEBUG=1`.
- Defaults to binding `127.0.0.1`. Set `WETNOSE_HOST=0.0.0.0` in `wetnose.env` to serve directly on the LAN.
- Systemd unit enables `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome`, `ProtectKernel*`, `RestrictSUIDSGID`, `LockPersonality`, `RestrictNamespaces`.
- `settings.json` and `logs/` are in `.gitignore` — webhook URLs and syslog targets are never committed.

---

## Requirements

- Python 3.10+
- Debian 12 / Ubuntu 22.04+ (or any systemd Linux)
- Outbound network access to `api.weather.gov`, `api.rainviewer.com`, `tilecache.rainviewer.com`, `opengeo.ncep.noaa.gov`, `nowcoast.noaa.gov`, `www.nhc.noaa.gov`, `mesonet.agron.iastate.edu` (radar tiles when the IEM source is selected, GeoJSON when Storm Cell Tracks is enabled), and `nominatim.openstreetmap.org` (only used when an admin clicks the pin "Find" button)

---

## Installation

### Quick start (development, Debian + venv)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git

git clone https://github.com/showsysdan/wetnoseweather.git
cd wetnoseweather

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

cp settings.example.json settings.json   # optional — /settings can also create it
cp wetnose.env.example   wetnose.env     # optional — defaults to 127.0.0.1:5100
python app.py
```

Open <http://localhost:5100/> for the landing page, <http://localhost:5100/settings> to configure, and <http://localhost:5100/output> for the kiosk display. To change the bind host or port, edit `WETNOSE_HOST` / `WETNOSE_PORT` in `wetnose.env`.

### Production (Debian + systemd + gunicorn)

Runs out of `~/wetnoseweather` under your regular user account (no dedicated service user). The shipped `wetnose.service` has `User=leko` and `WorkingDirectory=/home/leko/wetnoseweather` — if your username isn't `leko`, edit those two lines (and the `EnvironmentFile=`, `ExecStart=`, `BindPaths=`, and log paths) before installing the unit, or do it once via `sudo systemctl edit --full wetnose`.

**1. System packages**
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

**2. Clone into your home directory**
```bash
git clone https://github.com/showsysdan/wetnoseweather.git ~/wetnoseweather
cd ~/wetnoseweather
```

**3. Python venv + dependencies**
```bash
python3 -m venv ~/wetnoseweather/venv
~/wetnoseweather/venv/bin/pip install --upgrade pip
~/wetnoseweather/venv/bin/pip install -r ~/wetnoseweather/requirements.txt
```

**4. Settings file**
```bash
cp ~/wetnoseweather/settings.example.json ~/wetnoseweather/settings.json
```

**5. Bootstrap config (host / port / secret key)**
```bash
# Generate a secret key
python3 -c "import secrets; print(secrets.token_hex(32))"

# Copy the env template and edit it
cp ~/wetnoseweather/wetnose.env.example ~/wetnoseweather/wetnose.env
$EDITOR ~/wetnoseweather/wetnose.env
# Set WETNOSE_SECRET_KEY=<value from above>
# Adjust WETNOSE_HOST / WETNOSE_PORT here if you need something other
# than 127.0.0.1:5100.

chmod 600 ~/wetnoseweather/wetnose.env   # secret key — keep it private
```

**6. Install + start the systemd unit**
```bash
sudo cp ~/wetnoseweather/wetnose.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wetnose.service
sudo systemctl status wetnose.service
```

The unit reads `~/wetnoseweather/wetnose.env` via `EnvironmentFile=` and uses `${WETNOSE_HOST}:${WETNOSE_PORT}` in its `--bind` argument, so port changes only require editing `wetnose.env` and `systemctl restart wetnose`. `Restart=on-failure` handles crashes; the output display polls `/api/health` from the browser every 5 s and fades to black if the server stops responding, so there's no separate host-side watchdog.

`ProtectHome=tmpfs` plus a single `BindPaths=` line gives the service access only to its own directory under `/home`, not to every other home dir on the box.

### Updates

```bash
cd ~/wetnoseweather
git pull
~/wetnoseweather/venv/bin/pip install -r requirements.txt
sudo systemctl restart wetnose.service
```

---

## Configuration Reference

There are **two** configuration files:

1. **`wetnose.env`** — bootstrap config that has to be known before the app starts: bind host, bind port, debug flag, Flask secret key. Same KEY=VALUE syntax as systemd `EnvironmentFile`. Read by both `python app.py` and the gunicorn systemd unit, so there is exactly one place to set the port. Copy `wetnose.env.example` and edit:

   ```bash
   cp wetnose.env.example wetnose.env
   $EDITOR wetnose.env
   ```

   | Variable | Default | Description |
   |----------|---------|-------------|
   | `WETNOSE_HOST` | `127.0.0.1` | Bind address. Set to `0.0.0.0` to listen on every interface for LAN access. |
   | `WETNOSE_PORT` | `5100` | Bind port, 1–65535. |
   | `WETNOSE_DEBUG` | `0` | Flask debug mode. Never enable in production. |
   | `WETNOSE_SECRET_KEY` | *(auto)* | Flask session key. Generate with `python3 -c "import secrets; print(secrets.token_hex(32))"`. If blank, a fresh key is generated on every process start (sessions don't survive restarts). |
   | `WETNOSE_WMS_CACHE_MB` | `2048` | Max disk size for the cached WMS tile pyramid under `cache/wms/`. Oldest tiles are evicted by mtime when the cap is exceeded. |

   `wetnose.env` is gitignored. Existing environment variables (e.g. systemd `Environment=` directives, or your shell) take precedence over values in the file.

2. **`settings.json`** — runtime settings managed through `/settings` and persisted to disk. Excluded from git — use `settings.example.json` as a template.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `radar_source` | string | `"rainviewer"` | `"rainviewer"`, `"nws"`, or `"iem"` (IEM super-res N0Q) |
| `rv_color` | int | `6` | RainViewer color scheme (0–8) |
| `opacity` | int | `70` | Radar layer opacity (0–100) |
| `anim_speed` | int | `500` | RainViewer frame interval, ms (50–5000) |
| `show_sidebar` | bool | `true` | Show alert sidebar on output display |
| `map_lat` | float | `28.5383` | Map center latitude |
| `map_lon` | float | `-81.3792` | Map center longitude |
| `map_zoom` | int | `8` | Leaflet zoom level (2–18) |
| `show_alert_polygons` | bool | `true` | Draw NWS warning polygons |
| `show_range_rings` | bool | `false` | Draw 50/100/200 nm rings |
| `range_ring_station` | string | `"KMLB"` | `"KMLB"` or `"KTBW"` |
| `show_satellite_ir` | bool | `false` | RainViewer IR satellite overlay |
| `show_hurricane` | bool | `true` | NHC/nowCOAST tropical-cyclone WMS |
| `nws_product` | string | `"conus_bref_qcd"` | NWS WMS product layer |
| `wms_frame_count` | int | `8` | Number of animated WMS frames (NWS/IEM only). 2–24. |
| `wms_frame_step_min` | int | `5` | Minutes between WMS frames (NWS/IEM only). 1–15. Total loop = count × step. |
| `anim_hold_last_ms` | int | `0` | Extra pause (ms) on the final frame before the loop restarts. 0–10000. Applies to all sources. |
| `webhook_url` | string | `""` | HTTP/HTTPS endpoint for alert POSTs |
| `webhook_min_severity` | string | `"Severe"` | `Extreme`/`Severe`/`Moderate`/`Minor` |
| `syslog_enabled` | bool | `false` | Enable UDP syslog output |
| `syslog_host` | string | `""` | Syslog server hostname or IP |
| `syslog_port` | int | `514` | Syslog UDP port (1–65535) |
| `syslog_facility` | string | `"local0"` | `local0`–`local7`, `user`, `daemon` |
| `pin_enabled` | bool | `false` | Draw a point-of-interest pin on the output display |
| `pin_lat` | float | `28.5383` | Pin latitude |
| `pin_lon` | float | `-81.3792` | Pin longitude |
| `pin_label` | string | `""` | Optional label rendered next to the pin (≤80 chars) |
| `pin_radius_enabled` | bool | `false` | Draw a dashed radius circle around the pin |
| `pin_radius_miles` | float | `10` | Radius in miles (0.1–500) |
| `show_storm_cells` | bool | `false` | NEXRAD storm cells with ~30-min projected tracks (IEM source) |
| `storm_cells_url` | string | *(IEM default)* | GeoJSON FeatureCollection URL. Defaults to `https://mesonet.agron.iastate.edu/geojson/nexrad_attr.geojson`. Override if the default 404s or you want a different feed. SSRF-checked: must be http/https and not a private/loopback address. |

### NWS WMS products

| Value | Description |
|-------|-------------|
| `conus_bref_qcd` | Base Reflectivity (default) |
| `conus_cref_qcd` | Composite Reflectivity |
| `conus_bvel_qcd` | Base Velocity |
| `conus_n1p_qcd` | 1-Hour Precipitation |
| `conus_ntp_qcd` | Storm-Total Precipitation |

### RainViewer color schemes

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
| 8 | (reserved by RainViewer) |

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/` | Landing page with health probe and quick links |
| `GET`  | `/output` | Kiosk display (1920×1080, locked) |
| `GET`  | `/settings` | Settings interface |
| `GET`  | `/api/health` | Server health ping — `{"status":"ok"}` |
| `GET`  | `/api/settings` | Current settings as JSON |
| `POST` | `/api/settings` | Save settings (validated); triggers output reload |
| `GET`  | `/api/alerts` | NWS active alerts for the configured location |
| `GET`  | `/api/alert_polygons` | GeoJSON polygons from active alerts |
| `GET`  | `/api/rainviewer` | Proxied RainViewer weather-maps manifest |
| `GET`  | `/api/hurricane` | NHC Atlantic / East Pacific active storms |
| `GET`  | `/api/radar_status` | VCP, operability, age, failover state for KMLB+KTBW |
| `GET`  | `/api/station_coords` | Lat/lon of KMLB and KTBW |
| `GET`  | `/api/geocode?q=...` | Resolve a US address to lat/lon via Nominatim (cached 24h, rate-limited) |
| `GET`  | `/api/storm_cells` | Proxied IEM NEXRAD storm-attributes GeoJSON (cached 30s, serves stale data if IEM is briefly unreachable) |
| `GET`  | `/api/wms_tile/<source>` | WMS tile proxy for `nws` and `iem`. Mirrors Leaflet's GetMap request to the upstream, caches the PNG on disk under `cache/wms/`, and serves from cache thereafter. Layer parameter whitelisted. Cache size capped by `WETNOSE_WMS_CACHE_MB` (default 2048). |
| `GET`  | `/api/logs?n=200` | Last N alert-log entries (max 500) |
| `POST` | `/api/logs/clear` | Truncate the alert log |

### Webhook payload

When a new alert meets the minimum severity threshold, a POST is sent to `webhook_url`:

```json
{
  "text": "Severe – Tornado Warning",
  "event": "Tornado Warning",
  "severity": "Severe",
  "headline": "Tornado Warning issued for Orange County FL until 9:45 PM EDT",
  "area": "Orange County FL",
  "expires": "2024-08-15T21:45:00-04:00"
}
```

The `text` field works for Slack and Teams incoming webhooks. For other systems, use the structured fields directly.

---

## External Data Sources

| Source | Used for | Update cadence |
|--------|----------|---------------|
| `api.rainviewer.com` | Radar frame manifest, satellite IR | ~6–10 min |
| `tilecache.rainviewer.com` | Radar / satellite tiles | Per frame |
| `api.weather.gov` | Active alerts, polygons | 5 min |
| `api.weather.gov/radar/stations/` | VCP, operability, RDA timestamp | 5 min |
| `opengeo.ncep.noaa.gov` | NWS CONUS radar WMS tiles | VCP-driven |
| `nowcoast.noaa.gov` | Hurricane track / cone WMS | ~10 min |
| `www.nhc.noaa.gov` | Active storm list | On page load |
| `mesonet.agron.iastate.edu` | IEM super-res N0Q radar tiles (when `radar_source="iem"`), NEXRAD storm-cell GeoJSON (optional) | WMS-T frames every 5 min; storm cells: 1 min poll, 30 s server cache |
| `nominatim.openstreetmap.org` | Address → lat/lon for pin (admin only, manual) | On demand |
| `basemaps.cartocdn.com` | Dark basemap tiles | Static CDN |

All outbound requests use a descriptive `User-Agent` (`WetNoseWeather/1.0`) as required by `api.weather.gov`.

---

## File Structure

```
wetnoseweather/
├── app.py                       # Flask app, API routes, alert logic
├── requirements.txt             # Pinned Python deps
├── settings.example.json        # Runtime settings template — copy to settings.json
├── wetnose.env.example          # Bootstrap config template (host/port/secret) — copy to wetnose.env
├── wetnose.service              # Systemd unit
├── .gitignore
├── README.md
├── templates/
│   ├── index.html               # Landing page
│   ├── output.html              # 1920×1080 kiosk display
│   └── settings.html            # Settings admin interface
├── logs/                        # Created at runtime, excluded from git
│   ├── alerts.jsonl             # Event log
│   ├── notified.json            # Webhook dedup state
│   └── active_station.txt       # Cross-worker failover state
└── cache/wms/                   # WMS tile cache (PNG, capped by WETNOSE_WMS_CACHE_MB)
```

---

## Changing Location

Map center and the alert area are both driven by `map_lat` / `map_lon` — change them via the mini-map in `/settings` and save.

For locations outside Central Florida you should also update the radar station fallback constants in `app.py`:

```python
RADAR_PRIMARY   = 'KMLB'   # Replace with your nearest WSR-88D site
RADAR_SECONDARY = 'KTBW'   # Replace with your secondary site

STATION_COORDS = {
    'KMLB': {'lat': 28.1128, 'lon': -80.6547, 'name': 'Melbourne FL'},
    'KTBW': {'lat': 27.7056, 'lon': -82.4019, 'name': 'Tampa Bay FL'},
}
```

A full list of WSR-88D site IDs is at <https://www.roc.noaa.gov/WSR88D/Maps.aspx>.

---

## Known Limitations

- **RainViewer lightning** — lightning tiles require a paid RainViewer key; the free public API serves radar + satellite IR only.
- **NWS WMS availability** — `opengeo.ncep.noaa.gov` is a government service and goes down occasionally, especially during heavy weather. The tile-error indicator surfaces this in the output.
- **Hurricane track** — the nowCOAST WMS renders nothing outside active storm seasons; this is expected.
- **Zone-only alerts** — not every NWS alert carries polygon geometry. Cards appear in the sidebar regardless; the polygon overlay just stays empty for those.

---

## Development Notes

- **Debug mode** — `WETNOSE_DEBUG=1 python app.py` enables Flask's reloader and debugger. Never enable in production.
- **No automated tests yet.** Contributions welcome.
- **CDN assets** — Leaflet 1.9.4 and Google Fonts are loaded from CDNs. For air-gapped deployments, vendor them locally.

---

## License

MIT — see `LICENSE`.
