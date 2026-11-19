#!/usr/bin/env bash
# Backup script for paper bot data with operational and full modes.
# Usage: ./scripts/backup.sh [options] [destination]
# Options:
#   --full          Create a full backup (operational data + .env and deployment configs).
#   --env           Include .env file in full backup (requires --full).
#   --list          List existing backups with timestamps and sizes.
#   --dry-run       Show actions without creating archives.
#   --help          Show this help message.
# Destination defaults to ./backups-manuali for operational backups
# or ./backups-complete for full backups.

set -euo pipefail
umask 077

cd "$(dirname "$0")/.."

# Parse arguments
FULL=0
INCLUDE_ENV=0
LIST=0
DRY_RUN=0
DEST=""
while [[ "$#" -gt 0 ]]; do
  case $1 in
    --full) FULL=1; shift;;
    --env) INCLUDE_ENV=1; shift;;
    --list) LIST=1; shift;;
    --dry-run) DRY_RUN=1; shift;;
    -h|--help|"--help")
      sed -n '1,25p' "$0"
      exit 0
      ;;
    *)
      if [[ -z "$DEST" ]]; then
        DEST="$1"
      else
        echo "Unexpected argument: $1" >&2
        exit 1
      fi
      shift
      ;;
  esac
done

# List mode - show available backups
if [[ $LIST -eq 1 ]]; then
  echo "Operational backups (backups-manuali):"
  if [[ -d "${DEST:-./backups-manuali}" ]]; then
    find "${DEST:-./backups-manuali}" -maxdepth 1 -type f -name 'paper-bot-data-*.tar.gz' -printf '%TY-%Tm-%Td %TH:%TM:%TS %p %s bytes\n' | sort -r
  fi
  echo "Full backups (backups-complete):"
  if [[ -d "${DEST:-./backups-complete}" ]]; then
    find "${DEST:-./backups-complete}" -maxdepth 1 -type f -name 'paper-bot-data-*.tar.gz' -printf '%TY-%Tm-%Td %TH:%TM:%TS %p %s bytes\n' | sort -r
  fi
  exit 0
fi

# Determine destination based on mode
if [[ $FULL -eq 1 ]]; then
  DEST="${DEST:-./backups-complete}"
else
  DEST="${DEST:-./backups-manuali}"
fi

mkdir -p "$DEST"

# Verify required directories exist
if [[ ! -d data ]]; then
  echo "ERROR: data/ directory not found in $(pwd)" >&2
  exit 1
fi

# Build archive name
ARCHIVE_TS=$(date +%Y%m%d-%H%M%S)
OUT="$DEST/paper-bot-data-${ARCHIVE_TS}.tar.gz"

# Dry-run mode
if [[ $DRY_RUN -eq 1 ]]; then
  echo "DRY RUN: Would create backup at: $OUT"
  echo "Mode: $([ $FULL -eq 1 ] && echo 'Full' || echo 'Operational')"
  if [[ $FULL -eq 1 ]]; then
    echo "  Includes: $([ $INCLUDE_ENV -eq 1 ] && echo '.env, ' || echo '')config/"
    echo "  File permissions: 600 (restricted)"
  fi
  echo "  Exclusions: backups/, *.log, bot.lock, *.tmp, .*.tmp"
  exit 0
fi

echo "Creating backup: $OUT"

# Create staging directory for atomic operation
STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT

# Build tar exclude options
EXCLUDE_ARGS=()
EXCLUDE_ARGS+=(--exclude='data/backups' --exclude='data/*.log' --exclude='data/*.log.*')
EXCLUDE_ARGS+=(--exclude='data/bot.lock' --exclude='data/*.tmp' --exclude='data/.*.tmp')

# Build include arguments for full backup
INCLUDE_ARGS=()
if [[ $FULL -eq 1 ]]; then
  if [[ $INCLUDE_ENV -eq 1 && -f .env ]]; then
    INCLUDE_ARGS+=(--transform='s|^|full/|' .env)
    echo "       WARNING: Including .env - this file contains credentials!"
    echo "       File permissions will be restricted (600) in the archive."
  fi
  if [[ -d config ]]; then
    INCLUDE_ARGS+=(--transform='s|^config/|full/config/|' config)
  fi
  # Add bot/config.yaml if it exists (critical for deployment recovery)
  if [[ -f bot/config.yaml ]]; then
    INCLUDE_ARGS+=(--transform='s|^bot/config.yaml|full/bot/config.yaml|' bot/config.yaml)
  fi
  # NOTE: data_seed/ directory no longer exists (brain seed removed).
  # The bot starts with default rules and learns from scratch.
fi

# Create temporary archive first (atomic operation)
TMP_OUT="${OUT}.tmp.$$"

if [[ $FULL -eq 1 && ${#INCLUDE_ARGS[@]} -gt 0 ]]; then
  # Full backup with additional files
  tar -czf "$TMP_OUT" "${EXCLUDE_ARGS[@]}" "${INCLUDE_ARGS[@]}" data
else
  # Operational backup (default)
  tar -czf "$TMP_OUT" "${EXCLUDE_ARGS[@]}" data
fi

set +e
mv "$TMP_OUT" "$OUT"
RESULT=$?
set -e

if [[ $RESULT -ne 0 ]]; then
  echo "ERROR: Failed to create backup archive" >&2
  exit 1
fi

# Set restrictive permissions (secrets protection)
chmod 600 "$OUT"

# Verify archive integrity and display cash correctly
if command -v python3 >/dev/null 2>&1; then
  python3 -c "
import sys, tarfile, json
with tarfile.open(sys.argv[1]) as t:
    names = t.getnames()
    if 'data/state.json' in names:
        s = json.load(t.extractfile('data/state.json'))
        cash = s.get('cash', 0)
        print(f'Backup verified: {sys.argv[1]}')
        print(f'  Cash: \${cash:.2f}')
        print(f'  Positions: {len(s.get(\"positions\", {}))}')
        print(f'  Closed trades: {len(s.get(\"closed\", []))}')
    else:
        print(f'Backup created: {sys.argv[1]} ({len(names)} entries, no state.json)')
" "$OUT"
else
  tar -tzf "$OUT" >/dev/null && echo "Backup created (JSON validation skipped): $OUT"
fi

# Document what's excluded
echo "Excluded from backup:"
echo "  - data/backups/ (internal automatic backups)"
echo "  - data/*.log, data/*.log.* (current log files)"
echo "  - data/bot.lock (runtime lock)"
echo "  - data/*.tmp, data/.*.tmp (temporary files)"

# Retention: keep last 30 backups per destination
find "$DEST" -maxdepth 1 -name 'paper-bot-data-*.tar.gz' -printf '%T@ %p\n' 2>/dev/null | sort -rn | tail -n +31 | cut -d' ' -f2- | while read -r f; do
  rm -f "$f"
done

echo "Backup complete: $OUT"