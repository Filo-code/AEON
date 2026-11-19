#!/usr/bin/env bash
set -euo pipefail

VERIFY_ONLY=0
DRY_RUN=0
SRC=""
while [[ "$#" -gt 0 ]]; do
  case $1 in
    --verify-only) VERIFY_ONLY=1; shift;;
    --dry-run) DRY_RUN=1; shift;;
    -h|--help|"--help")
      sed -n '1,25p' "$0"
      exit 0
      ;;
    *)
      if [[ -z "$SRC" ]]; then
        SRC="$1"
      else
        echo "Unexpected argument: $1" >&2
        exit 1
      fi
      shift
      ;;
  esac
done

if [[ -z "$SRC" ]]; then
  echo "ERROR: source archive or directory required" >&2
  exit 1
fi

cd "$(dirname "$0")/.."

if [[ -f data/bot.lock ]]; then
  if command -v flock >/dev/null 2>&1; then
    if ! flock -n data/bot.lock -c true 2>/dev/null; then
      echo "ERROR: bot is running (data/bot.lock is held). Stop the bot first." >&2
      exit 1
    fi
  fi
fi

TS=$(date +%Y%m%d-%H%M%S)
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

echo "[1/5] Preparing and verifying backup: $SRC"

if [[ "$SRC" == *".."* ]] || [[ "$SRC" == *"//"* ]]; then
  echo "ERROR: Invalid path (path traversal attempt detected)" >&2
  exit 1
fi

if [[ -f "$SRC" ]]; then
  if ! file "$SRC" | grep -q 'gzip compressed'; then
    echo "ERROR: Source is not a valid gzip archive" >&2
    exit 1
  fi
  if ! tar -xzf "$SRC" -C "$STAGE" --no-same-owner --no-same-permissions 2>/dev/null; then
    echo "ERROR: failed to extract archive" >&2
    exit 1
  fi
  if [[ ! -d "$STAGE/data" ]]; then
    echo "ERROR: archive does not contain data/ directory" >&2
    exit 1
  fi
  NEW="$STAGE/data"
elif [[ -d "$SRC" ]]; then
  mkdir -p "$STAGE/data"
  if find "$SRC" -type l \( -lname "/*" -o -lname "../*" \) 2>/dev/null | grep -q .; then
    echo "ERROR: Dangerous symlinks detected in source" >&2
    exit 1
  fi
  cp -a "$SRC"/. "$STAGE/data/"
  NEW="$STAGE/data"
else
  echo "ERROR: source not found: $SRC" >&2
  exit 1
fi

if [[ ! -f "$NEW/state.json" ]]; then
  echo "ERROR: state.json not found in backup" >&2
  exit 1
fi

VERIFY_OUTPUT=$(mktemp)
VERIFY_RESULT=0
if command -v python3 >/dev/null 2>&1; then
  PY_SCRIPT=$(mktemp --suffix=.py)
  cat > "$PY_SCRIPT" << 'PYEND'
import json, sys, os
try:
    state_path = sys.argv[1]
    with open(state_path) as f:
        s = json.load(f)
    assert isinstance(s, dict), 'state.json must be a JSON object'
    assert 'cash' in s, 'state.json must contain cash field'
    assert 'positions' in s, 'state.json must contain positions field'
    assert 'closed' in s, 'state.json must contain closed field'
    cash = s.get('cash', 0)
    if not isinstance(cash, (int, float)) or isinstance(cash, bool):
        raise ValueError(f'cash must be a number, got {type(cash).__name__}')
    positions = s.get('positions', {})
    if not isinstance(positions, dict):
        raise ValueError(f'positions must be a dict, got {type(positions).__name__}')
    closed = s.get('closed', [])
    if not isinstance(closed, list):
        raise ValueError(f'closed must be a list, got {type(closed).__name__}')
    print('state.json VALID')
    print(f'Cash: ${s["cash"]:.2f}')
    print(f'Open positions: {len(positions)}')
    print(f'Closed trades: {len(closed)}')
except json.JSONDecodeError as e:
    print(f'INVALID JSON: {e}', file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f'ERROR: {e}', file=sys.stderr)
    sys.exit(1)
PYEND
  python3 "$PY_SCRIPT" "$NEW/state.json" > "$VERIFY_OUTPUT" 2>&1 || VERIFY_RESULT=$?
  rm -f "$PY_SCRIPT"
fi

if [[ $VERIFY_RESULT -ne 0 ]]; then
  echo "ERROR: backup verification failed:" >&2
  cat "$VERIFY_OUTPUT" >&2
  rm "$VERIFY_OUTPUT"
  exit 1
fi
rm "$VERIFY_OUTPUT"

if [[ $VERIFY_ONLY -eq 1 ]]; then
  echo "[3/5] Verification complete - backup is valid"
  exit 0
fi

if [[ $DRY_RUN -eq 1 ]]; then
  echo "[3/5] DRY RUN"
  exit 0
fi

echo "[3/5] Creating safety backup of current data..."
SAFETY_BACKUP=""
if [[ -d data ]]; then
  SAFETY_BACKUP="data.before-restore-$TS"
  mkdir -p "$SAFETY_BACKUP"
  find data -mindepth 1 -maxdepth 1 ! -name backups ! -name .restore.lock -exec cp -a {} "$SAFETY_BACKUP"/ +
fi

RESTORE_LOCK="data/.restore.lock"
mkdir -p data
LOCK_ACQUIRED=0
if command -v flock >/dev/null 2>&1; then
  exec 9>"$RESTORE_LOCK"
  if flock -n 9 2>/dev/null; then
    LOCK_ACQUIRED=1
  else
    exit 1
  fi
fi

TMP_RESTORE="data.tmp-restore-$TS"
mkdir -p "$TMP_RESTORE"
cp -a "$NEW"/. "$TMP_RESTORE"/

# Merge incoming backups without overwriting existing local backups
if [[ -d data/backups ]]; then
  mkdir -p "$TMP_RESTORE/backups"
  cp -an data/backups/. "$TMP_RESTORE/backups/" 2>/dev/null || cp -a data/backups/. "$TMP_RESTORE/backups/"
fi

PRE_SWAP_OK=1
if command -v python3 >/dev/null 2>&1; then
  if python3 -c "import json; s=json.load(open('${TMP_RESTORE}/state.json')); assert isinstance(s, dict); assert 'cash' in s and isinstance(s['cash'], (int, float)); assert 'positions' in s and isinstance(s['positions'], dict); assert 'closed' in s and isinstance(s['closed'], list)" 2>/dev/null; then
    PRE_SWAP_OK=0
  else
    PRE_SWAP_OK=1
  fi
else
  PRE_SWAP_OK=0
fi

if [[ $PRE_SWAP_OK -ne 0 ]]; then
  echo "ERROR: Restored data verification failed before swap" >&2
  rm -rf "$TMP_RESTORE"
  if [[ "$LOCK_ACQUIRED" -eq 1 ]] && command -v flock >/dev/null 2>&1; then flock -u 9; fi
  rm -f "$RESTORE_LOCK"
  exit 1
fi

if [[ -d "$TMP_RESTORE" ]] && [[ -f "$TMP_RESTORE/state.json" ]]; then
  find data -mindepth 1 -maxdepth 1 ! -name .restore.lock -exec rm -rf {} + 2>/dev/null || true
  cp -a "$TMP_RESTORE"/. data/
  rm -rf "$TMP_RESTORE"
else
  echo "ERROR: Temp restore directory or state.json not found" >&2
  rm -rf "$TMP_RESTORE"
  if [[ -n "$SAFETY_BACKUP" && -d "$SAFETY_BACKUP" ]]; then
    find data -mindepth 1 -maxdepth 1 ! -name backups ! -name .restore.lock -exec rm -rf {} + 2>/dev/null || true
    cp -a "$SAFETY_BACKUP"/. data/ 2>/dev/null || true
  fi
  if [[ "$LOCK_ACQUIRED" -eq 1 ]] && command -v flock >/dev/null 2>&1; then flock -u 9; fi
  rm -f "$RESTORE_LOCK"
  exit 1
fi

rm -f data/heartbeat.json data/bot.lock data/state.json.tmp 2>/dev/null || true
if [[ "$LOCK_ACQUIRED" -eq 1 ]] && command -v flock >/dev/null 2>&1; then
  flock -u 9
fi
rm -f "$RESTORE_LOCK"
echo "Restore complete. Safety backup: $SAFETY_BACKUP"
