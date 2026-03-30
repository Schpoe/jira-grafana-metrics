#!/bin/bash
set -e

# Make env vars available to cron (cron runs with a clean environment)
printenv | grep -E "^(JIRA_|POSTGRES_)" > /etc/environment

# Run an initial sync immediately on container start
echo "[entrypoint] Running initial sync..."
python /app/sync.py

# Hand off to cron
echo "[entrypoint] Starting cron scheduler (08:00 and 20:00 CET)..."
exec cron -f
