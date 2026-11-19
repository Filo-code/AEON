# AEON — VPS Deployment Readiness Audit

**Author:** Senior Quant Developer & Trading Systems Auditor  
**Date:** 2026-11-18  
**Target Environment:** Ubuntu 24.04 LTS (Hetzner Cloud / Dedicated Small VPS)  
**Status:** READY WITH PRE-FLIGHT WARNINGS  

---

## 1. Operating System & Runtime Compatibility

| Component | Target Requirement | Current Project Status | Verdict |
|---|---|---|---|
| **OS Distribution** | Ubuntu 24.04 LTS (Noble Numbat) | Pinned base in `Dockerfile` (`python:3.12.8-slim-bookworm` / Debian 12 equivalent); `deploy/install_vps.sh` and `install.sh` specifically written for Ubuntu 24.04 LTS. | **COMPATIBLE** |
| **Python Version** | Python 3.12+ (standard on 24.04) | All 53 Python files compile and pass tests on Python 3.12.10. | **COMPATIBLE** |
| **System Packaging** | `systemd` v255+ | Service templates use standard modern directives (`ProtectSystem=strict`, `ProtectHome=read-only`, `ReadWritePaths=${DATA_DIR}`, `MemoryMax=1G`). | **COMPATIBLE** |
| **Container Engine** | Docker 24+ & Docker Compose v2 | Validated multi-stage `Dockerfile` and `docker-compose.yml` with healthchecks. | **COMPATIBLE** |

---

## 2. Resource Footprint & Small VPS Sizing

Designed for small VPS instances (e.g., Hetzner CX22 / CPX21: 2 vCPU, 4 GB RAM, 40 GB NVMe):

- **Bot Service Memory Limit:** `MemoryMax=1G`, `MemoryHigh=800M` in systemd (1 GB in Docker Compose). Accommodates headless Chromium for the hourly dashboard screenshot.
- **Dashboard Service Memory Limit:** `MemoryMax=256M`, `MemoryHigh=128M` in systemd (256 MB in Docker Compose).
- **CPU Quota:** `CPUQuota=80%` prevents bot thread lockups from starving the host OS.
- **Disk I/O Protection:** `bot/disk_guard.py` enforces minimum free disk thresholds (100 MB minimum, default 500 MB safe threshold) before allowing state saves or backups.

---

## 3. Environment Variables & Secret Configuration

To deploy, copy `.env.example` to `.env` on the VPS and configure:

| Variable | Description | Default / Requirement | Security Note |
|---|---|---|---|
| `DATA_DIR` | Absolute path to persistent state directory | `/app/data` (Docker) or `/opt/aeon-by-rekt/data` (systemd) | Never mount over non-persistent storage. |
| `DASHBOARD_PASSWORD` | Basic auth password for web dashboard | **MANDATORY** | Bot and dashboard will refuse to start if empty or default. |
| `DASHBOARD_BIND` | Network bind address for dashboard | `127.0.0.1` | **CRITICAL:** Keep `127.0.0.1`. Never expose to `0.0.0.0` without HTTPS/Caddy reverse proxy. |
| `PORT` | Dashboard HTTP port | `8080` | Internal port. |
| `HELIUS_API_KEY` | Primary Solana RPC key | Optional (simulation works with public/fallback) | Rate limits apply if using public RPC. |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot token for notifications | Optional | If not set, notifications are written to local logs. |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID for alerts | Optional | Required if Telegram token is provided. |

---

## 4. Startup & Verification Procedures

### Systemd Deployment (Recommended)
1. Unpack repository to target host:
   ```bash
   sudo bash install.sh
   ```
2. Populate `/opt/aeon-by-rekt/.env`:
   ```bash
   sudo nano /opt/aeon-by-rekt/.env
   # Set DASHBOARD_PASSWORD=<strong_password>
   ```
3. Enable and start services:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now aeon-bot.service aeon-dashboard.service
   ```
4. Verify execution:
   ```bash
   sudo systemctl status aeon-bot.service aeon-dashboard.service
   sudo journalctl -u aeon-bot.service -f
   ```

### Docker Compose Deployment
1. Build and launch:
   ```bash
   docker compose up -d --build
   ```
2. Verify running containers:
   ```bash
   docker compose ps
   docker compose logs -f bot
   ```
3. Test healthcheck endpoint:
   ```bash
   curl -s http://127.0.0.1:8080/healthz | jq
   ```

---

## 5. Persistence, State Recovery & Backups

- **Atomic Writes:** `storage.py` guarantees state updates are written to temporary files, fsynced to NVMe, and renamed atomically.
- **Automated Snapshots:** `storage.Backups` generates hourly compressed tarballs in `data/backups/`.
- **Crash Recovery:** If `state.json` is missing or corrupted, `load_json_safe()` automatically scans `data/backups/` and recovers the most recent valid snapshot.
- **Manual Backup:** `bash scripts/backup.sh` creates timestamped backups in `backups-manuali/`.
- **Restore:** `bash scripts/restore.sh <tarball_path>` safely rolls back portfolio state.

---

## 6. Pre-Flight Warnings & Deployment Blockers

Before running live on the VPS, the operator must address the following warnings:

1. **Dashboard Password Requirement (`DASHBOARD_PASSWORD`):**
   `startup_checks.py` will abort startup if `DASHBOARD_PASSWORD` is not set in `.env`. This is a mandatory safety feature.
2. **Dashboard Binding Exposure (`DASHBOARD_BIND`):**
   The dashboard must remain bound to `127.0.0.1`. If external access is required, access via SSH tunnel (`ssh -L 8080:127.0.0.1:8080 user@vps`) or enable the `https` profile with Caddy (`docker compose --profile https up -d`).
3. **RPC Rate Limits:**
   If no dedicated RPC endpoint (Helius/Chainstack) is specified, the bot relies on public Solana RPCs, which may trigger 429 rate limit errors during high-volume periods.
4. **Simulation Mode Validation:**
   Ensure `simulation_only: true` in `bot/config.yaml` is NEVER toggled to false.
