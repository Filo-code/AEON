#!/usr/bin/env bash
# Installazione su Ubuntu 24.04 (Hetzner) con systemd: venv, Chrome headless per lo screenshot orario,
# utente dedicato, servizi bot + dashboard con riavvio automatico. Da lanciare come root dalla cartella scompattata.
# NON avvia il bot finché il .env non ha una password per la dashboard. Nessuna chiave viene scritta qui.
# Version: 3.0 - Systemd-only mode, complete rollback, service verification
set -euo pipefail

# Color output helpers
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1" >&2; }

# Check if running as root
[ "$(id -u)" = 0 ] || { log_error "Lancialo come root: sudo bash deploy/install_vps.sh"; exit 1; }

# === MODE SELECTION ===
# This script ONLY installs Systemd services.
# For Docker, use: docker compose up -d --build
MODE="systemd"
log_info "Modalità installazione: $MODE (solo Systemd)"
log_info "Per Docker: usa 'docker compose up -d --build'"

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DST=/opt/solana-paper-bot
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
export DEBIAN_FRONTEND=noninteractive

# === PREREQUISITE CHECKS ===
echo ""
echo "=== 0/8 Controllo prerequisiti"

# Check disk space (need at least 5GB free)
DISK_FREE=$(df -BG /opt 2>/dev/null | awk 'NR==2 {gsub(/G/,"",$4); print $4}' || echo 0)
if [ "$DISK_FREE" -lt 5 ]; then
  log_error "Spazio disco insufficiente: serve almeno 5GB liberi, ne ho ${DISK_FREE}GB"
  exit 1
fi

# Check required files exist in source (Systemd mode doesn't need Dockerfile/docker-compose.yml)
log_info "Verifico file richiesti in $SRC..."
REQUIRED_FILES=("requirements.txt" ".env.example" "bot/paper_bot.py" "systemd/aeon-bot.service.template" "systemd/aeon-dashboard.service.template")
MISSING_FILES=()
for f in "${REQUIRED_FILES[@]}"; do
  if [[ ! -f "$SRC/$f" ]]; then
    MISSING_FILES+=("$f")
  fi
done
if [ ${#MISSING_FILES[@]} -gt 0 ]; then
  log_error "File mancanti: ${MISSING_FILES[*]}"
  log_error "Questa release non è completa. Verifica che tutti i file siano presenti."
  exit 1
fi

# Check Python version
PYTHON_VER=$(python3 --version 2>&1 | awk '{print $2}' | cut -d. -f1,2)
log_info "Python version: $PYTHON_VER"

# Check if port 8080 is available
if ss -tlnp 2>/dev/null | grep -q ':8080 ' || netstat -tlnp 2>/dev/null | grep -q ':8080 '; then
  log_warn "Porta 8080 già in uso - verifiché che non ci siano altri servizi"
fi

# Save existing installation info for rollback
BACKUP_DIR=""
if [ -d "$DST" ]; then
  log_info "Trovata installazione precedente in $DST"
fi

echo ""
echo "=== 1/8 Pacchetti di sistema"
apt-get update -q
apt-get install -y -q python3-venv python3-pip unzip curl ca-certificates \
    fonts-dejavu-core fonts-noto-cjk fonts-noto-color-emoji \
    iproute2 procps || log_warn "Alcuni pacchetti potrebbero non essere disponibili"

echo ""
echo "=== 2/8 Browser headless per lo screenshot (se manca, il bot manda un riepilogo testuale)"
if ! command -v google-chrome-stable >/dev/null 2>&1 && ! command -v chromium >/dev/null 2>&1; then
  if [ "$(dpkg --print-architecture)" = amd64 ]; then
    curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb || {
      log_warn "Download Chrome fallito, continuo senza browser"
    }
    if [ -f /tmp/chrome.deb ]; then
      apt-get install -y -q /tmp/chrome.deb && rm -f /tmp/chrome.deb
    fi
  else
    apt-get install -y -q chromium 2>/dev/null || log_warn "!! Chromium non installato (ARM): userò il riepilogo testuale"
  fi
fi

echo ""
echo "=== 3/8 Backup e utente in $DST"
# Create backup of existing installation BEFORE updating
if [ -d "$DST" ]; then
  BACKUP_DIR="/opt/solana-paper-bot-backup-$TIMESTAMP"
  log_info "Creazione backup della versione esistente in $BACKUP_DIR..."
  cp -a "$DST" "$BACKUP_DIR" || { log_error "Backup fallito"; exit 1; }
  echo "$TIMESTAMP" > "$BACKUP_DIR/.backup_timestamp"
fi

# Create user if needed (idempotent)
if ! id paperbot >/dev/null 2>&1; then
  useradd -r -m -d "$DST" -s /usr/sbin/nologin paperbot
  log_info "Utente paperbot creato"
else
  log_info "Utente paperbot già esistente"
fi

mkdir -p "$DST"
if [ "$SRC" != "$DST" ]; then
  # Update code WITHOUT touching .env and data/ (preserve existing files)
  tar -C "$SRC" --exclude=./.env --exclude=./data --exclude=./.venv --exclude=./install.sh -cf - . | tar -C "$DST" -xf -
fi

cd "$DST"

# Only copy .env.example if .env doesn't exist (don't overwrite existing!)
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  log_info "Creato .env da .env.example"
else
  log_info ".env già esistente, conservato"
fi

# Create data directory if needed
mkdir -p data
# Ensure dashboard stays on 127.0.0.1 only
sed -i 's/^HOST=.*/HOST=127.0.0.1/' .env 2>/dev/null || true

echo ""
echo "=== 4/8 Ambiente Python"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt || {
  log_error "Installazione dipendenze fallita"
  # Attempt rollback
  if [ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ]; then
    log_warn "Ripristino versione precedente..."
    rm -rf "$DST"
    cp -a "$BACKUP_DIR" "$DST"
  fi
  exit 1
}
chown -R paperbot:paperbot "$DST"

echo ""
echo "=== 5/8 Servizi systemd (riavvio automatico)"
# Use the template files (they are the canonical ones)
if [ -f systemd/aeon-bot.service.template ] && [ -f systemd/aeon-dashboard.service.template ]; then
  # Generate actual service files from templates
  INSTALL_DIR="$DST"
  SERVICE_USER="paperbot"
  SERVICE_GROUP="paperbot"
  DATA_DIR="$DST/data"

  sed \
    -e "s|\${SERVICE_USER}|$SERVICE_USER|g" \
    -e "s|\${SERVICE_GROUP}|$SERVICE_GROUP|g" \
    -e "s|\${INSTALL_DIR}|$INSTALL_DIR|g" \
    -e "s|\${DATA_DIR}|$DATA_DIR|g" \
    systemd/aeon-bot.service.template > /etc/systemd/system/paper-bot.service

  sed \
    -e "s|\${SERVICE_USER}|$SERVICE_USER|g" \
    -e "s|\${SERVICE_GROUP}|$SERVICE_GROUP|g" \
    -e "s|\${INSTALL_DIR}|$INSTALL_DIR|g" \
    -e "s|\${DATA_DIR}|$DATA_DIR|g" \
    systemd/aeon-dashboard.service.template > /etc/systemd/system/paper-dashboard.service

  log_info "Servizi systemd installati da template"
else
  log_error "File template systemd non trovati"
  exit 1
fi

systemctl daemon-reload
systemctl enable paper-bot paper-dashboard || {
  log_error "Impossibile abilitare i servizi systemd"
  exit 1
}

echo ""
echo "=== 6/8 Controlli post-installazione"
# Verify installation
log_info "Verifico l'installazione..."
if [ -x .venv/bin/python ]; then
  log_info "Python virtuale: OK"
else
  log_error "Python virtuale non funzionante"
  exit 1
fi

if [ -f .env ] && [ -f requirements.txt ]; then
  log_info "File critici: OK"
else
  log_error "File critici mancanti"
  exit 1
fi

# Verify service files are loaded
if systemctl is-enabled paper-bot >/dev/null 2>&1 && systemctl is-enabled paper-dashboard >/dev/null 2>&1; then
  log_info "Servizi systemd abilitati: OK"
else
  log_error "Servizi systemd non abilitati correttamente"
  exit 1
fi

# Set restore marker for rollback capability
echo "$TIMESTAMP" > .install_timestamp

echo ""
echo "=== 7/8 Verifica servizi"
# Test service start (but don't keep running if config incomplete)
if grep -qE '^DASHBOARD_PASSWORD=.{8,}' .env && grep -qE '^TELEGRAM_BOT_TOKEN=.+' .env; then
  log_info "Configurazione completa - avvio servizi di test..."
  systemctl start paper-bot paper-dashboard || {
    log_error "Impossibile avviare i servizi"
    exit 1
  }
  sleep 3
  if systemctl is-active paper-bot >/dev/null 2>&1 && systemctl is-active paper-dashboard >/dev/null 2>&1; then
    log_info "Servizi avviati correttamente"
    systemctl stop paper-bot paper-dashboard
  else
    log_error "Servizi non attivi dopo l'avvio"
    systemctl status paper-bot paper-dashboard --no-pager || true
    exit 1
  fi
else
  log_warn "Configurazione incompleta - servizi non avviati"
fi

echo ""
echo "=== 8/8 Stato finale"
if grep -qE '^DASHBOARD_PASSWORD=.{8,}' .env && grep -qE '^TELEGRAM_BOT_TOKEN=.+' .env; then
  log_info "Configurazione completa - servizi pronti per l'avvio"
  log_info "Esegui: systemctl restart paper-bot paper-dashboard"
else
  log_warn "Configurazione incompleta"
  echo ""
  log_info "Compila il .env:"
  log_info "  nano $DST/.env"
  log_info "  (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DASHBOARD_PASSWORD, ...)"
  echo ""
  log_info "Poi avvia:"
  log_info "  systemctl restart paper-bot paper-dashboard"
fi

# Record successful installation
echo "$TIMESTAMP" >> .install_history

echo ""
log_info "Installazione completata!"
log_info "Note: Non modifico firewall, SSH, Docker, n8n o Caddy"
log_info "Dashboard è configurata per ascoltare solo su 127.0.0.1:8080"
