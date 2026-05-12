#!/usr/bin/env bash
# ── watchdog.sh ───────────────────────────────────────────────────────────────
# Lightweight health check for the NEXRAD service.
# Systemd's Restart=on-failure handles crashes; this catches hangs/deadlocks.
#
# Recommended: run via cron every 5 minutes as root or the nexrad user:
#   sudo crontab -e
#   */5 * * * * /opt/nexrad/watchdog.sh
#
# Or as a systemd timer — see nexrad-watchdog.timer (create separately).
# ─────────────────────────────────────────────────────────────────────────────

HEALTH_URL="http://127.0.0.1:5000/api/health"
SERVICE="nexrad"
TIMEOUT=5
LOGGER_TAG="nexrad-watchdog"

if curl -sf --max-time "$TIMEOUT" "$HEALTH_URL" >/dev/null 2>&1; then
    # Healthy — do nothing
    exit 0
fi

# Unhealthy — log and restart
logger -t "$LOGGER_TAG" "Health check at $HEALTH_URL failed. Restarting $SERVICE."

if systemctl restart "$SERVICE"; then
    logger -t "$LOGGER_TAG" "Successfully restarted $SERVICE."
else
    logger -t "$LOGGER_TAG" "ERROR: Failed to restart $SERVICE. Manual intervention required."
    exit 1
fi
