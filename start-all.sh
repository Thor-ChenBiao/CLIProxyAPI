#!/bin/bash
# CLIProxyAPI startup script - starts all services via supervisor
# Add to crontab: @reboot /root/CLIProxyAPI/start-all.sh

LOG="/tmp/startup-all.log"
echo "$(date) - Starting all services..." >> "$LOG"

# Wait for network
sleep 5

# Kill any stale processes
pkill -f "supervisord" 2>/dev/null
sleep 2

# Start supervisor (manages cliproxyapi, litellm, key-portal)
/usr/bin/supervisord -c /etc/supervisor/supervisord.conf >> "$LOG" 2>&1

sleep 5
/usr/bin/supervisorctl status >> "$LOG" 2>&1
echo "$(date) - Startup complete" >> "$LOG"
