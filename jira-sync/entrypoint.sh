#!/bin/bash
set -e

# Make env vars available to cron (cron runs with a clean environment)
printenv | grep -E "^(JIRA_|POSTGRES_|NOTIFY_)" > /etc/environment

# Pipe cron job log to container stdout so it appears in docker compose logs
touch /var/log/jira-sync.log
tail -F /var/log/jira-sync.log &

# Run an initial sync immediately on container start
echo "[entrypoint] Running initial sync..."
/usr/local/bin/python /app/sync.py

# Hand off to cron
echo "[entrypoint] Starting cron scheduler (07:00 and 19:00 UTC)..."
exec cron -f
