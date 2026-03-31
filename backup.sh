#!/usr/bin/env bash
# =============================================================================
# Jira Metrics — Daily Backup Script
# Backs up PostgreSQL + Grafana to a network share on 192.168.1.5
#
# Setup:
#   1. Copy .backup.conf.example to .backup.conf and fill in credentials
#   2. Install cifs-utils:  sudo apt install cifs-utils
#   3. Make executable:     chmod +x backup.sh
#   4. Add cron job:        sudo crontab -e
#      Add line:  0 22 * * * TZ=Europe/Berlin /path/to/jira-grafana-metrics/backup.sh
# =============================================================================

set -euo pipefail

# ── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_FILE="$SCRIPT_DIR/.backup.conf"
LOG_FILE="$SCRIPT_DIR/backup.log"
MOUNT_POINT="/mnt/jira-metrics-nas"

# ── Load config ──────────────────────────────────────────────────────────────
if [[ ! -f "$CONF_FILE" ]]; then
    echo "ERROR: $CONF_FILE not found. Copy .backup.conf.example and fill in your NAS credentials." >&2
    exit 1
fi
# shellcheck source=.backup.conf
source "$CONF_FILE"

# Required vars from config
: "${NAS_USER:?NAS_USER not set in .backup.conf}"
: "${NAS_PASS:?NAS_PASS not set in .backup.conf}"
: "${RETENTION_DAYS:=30}"

NAS_SHARE="//192.168.1.5/downloads"
NAS_PATH="backups/jira-metrics"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(grep POSTGRES_PASSWORD "$SCRIPT_DIR/.env" | cut -d= -f2)}"

# ── Logging ──────────────────────────────────────────────────────────────────
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }
die() { log "ERROR: $*"; exit 1; }

# ── Cleanup on exit ──────────────────────────────────────────────────────────
TEMP_DIR=""
cleanup() {
    if mountpoint -q "$MOUNT_POINT" 2>/dev/null; then
        umount "$MOUNT_POINT" 2>/dev/null || true
    fi
    [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]] && rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

# =============================================================================
log "=== Backup started ==="

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_NAME="jira-metrics-$TIMESTAMP"
TEMP_DIR=$(mktemp -d)
BACKUP_DIR="$TEMP_DIR/$BACKUP_NAME"
mkdir -p "$BACKUP_DIR"

# ── 1. PostgreSQL dump ────────────────────────────────────────────────────────
log "Dumping PostgreSQL..."
docker compose -f "$SCRIPT_DIR/docker-compose.yml" exec -T postgres \
    pg_dump -U metrics -d jira_metrics \
    | gzip > "$BACKUP_DIR/postgres.sql.gz" \
    || die "PostgreSQL dump failed"
log "  postgres.sql.gz: $(du -sh "$BACKUP_DIR/postgres.sql.gz" | cut -f1)"

# ── 2. Grafana volume ─────────────────────────────────────────────────────────
log "Backing up Grafana data volume..."
docker run --rm \
    -v jira-grafana-metrics_grafana_data:/data:ro \
    -v "$BACKUP_DIR":/backup \
    alpine tar czf /backup/grafana-data.tar.gz -C /data . \
    || die "Grafana volume backup failed"
log "  grafana-data.tar.gz: $(du -sh "$BACKUP_DIR/grafana-data.tar.gz" | cut -f1)"

# ── 3. Config snapshot ────────────────────────────────────────────────────────
log "Snapshotting config files..."
# Mask the actual secrets — just record which vars are set
grep -oP '^[A-Z_]+(?==)' "$SCRIPT_DIR/.env" > "$BACKUP_DIR/env-keys.txt" 2>/dev/null || true
cp "$SCRIPT_DIR/docker-compose.yml" "$BACKUP_DIR/docker-compose.yml"
log "  Config snapshot done"

# ── 4. Create archive ─────────────────────────────────────────────────────────
log "Creating archive..."
ARCHIVE="/tmp/${BACKUP_NAME}.tar.gz"
tar czf "$ARCHIVE" -C "$TEMP_DIR" "$BACKUP_NAME"
ARCHIVE_SIZE=$(du -sh "$ARCHIVE" | cut -f1)
log "  Archive: $ARCHIVE ($ARCHIVE_SIZE)"

# ── 5. Mount NAS and copy ─────────────────────────────────────────────────────
log "Mounting NAS share $NAS_SHARE..."
mkdir -p "$MOUNT_POINT"
mount -t cifs "$NAS_SHARE" "$MOUNT_POINT" \
    -o "username=$NAS_USER,password=$NAS_PASS,uid=$(id -u),gid=$(id -g),file_mode=0660,dir_mode=0770" \
    || die "Failed to mount NAS share. Check NAS_USER/NAS_PASS and that 192.168.1.5 is reachable."

REMOTE_DIR="$MOUNT_POINT/$NAS_PATH"
mkdir -p "$REMOTE_DIR"

log "Copying backup to NAS..."
cp "$ARCHIVE" "$REMOTE_DIR/" || die "Copy to NAS failed"
log "  Copied to $REMOTE_DIR/${BACKUP_NAME}.tar.gz"

# ── 6. Retention: delete backups older than RETENTION_DAYS ───────────────────
log "Applying retention policy (${RETENTION_DAYS} days)..."
DELETED=0
while IFS= read -r old_file; do
    rm -f "$old_file"
    log "  Deleted old backup: $(basename "$old_file")"
    ((DELETED++))
done < <(find "$REMOTE_DIR" -name "jira-metrics-*.tar.gz" -mtime +"$RETENTION_DAYS" 2>/dev/null)
log "  Retention: removed $DELETED old backup(s)"

# ── 7. Summary ────────────────────────────────────────────────────────────────
BACKUP_COUNT=$(find "$REMOTE_DIR" -name "jira-metrics-*.tar.gz" 2>/dev/null | wc -l)
log "NAS now holds $BACKUP_COUNT backup(s)"
log "=== Backup complete: $BACKUP_NAME ($ARCHIVE_SIZE) ==="

# Cleanup temp archive
rm -f "$ARCHIVE"
