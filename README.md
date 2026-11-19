# AEON — Autonomous Simulated Trading System & Intelligence Engine

AEON is an autonomous Solana paper-trading system engineered for simulation, algorithmic execution analysis, and progressive strategy optimization. It combines real-time multi-provider market telemetry (Pump.fun, GMGN, GeckoTerminal, DexScreener), a fail-closed Anti-Rug engine, a constrained adaptive learning brain (Cervello AI), strict capital risk guards, and a real-time observability dashboard.

> [!IMPORTANT]
> **SIMULATION ONLY:** AEON operates strictly in paper-trading simulation mode. No real capital, private keys, or mainnet funds are ever risked.

---

## Architecture & Repository Structure

```text
AOEN/
├── bot/                              # Core Trading Engine & Algorithms
│   ├── paper_bot.py                  # Main event loop & paper execution engine
│   ├── config.yaml                   # Baseline configuration & strategy parameters
│   ├── risk.py                       # Risk Manager (survival mode, exposure limits)
│   ├── antirug.py                    # Anti-Rug Security Engine (fail-closed token validator)
│   ├── cervello.py                   # Adaptive learning brain (walk-forward optimization)
│   ├── storage.py                    # State persistence, atomic writes & snapshot backups
│   ├── net.py                        # Resilient HTTP client with circuit breakers & retry
│   ├── rpc_manager.py                # Multi-provider RPC pool with latency & health tracking
│   ├── birdeye.py                    # Birdeye API client
│   ├── gmgn.py / gmgn_adapter.py     # GMGN WebSocket & REST telemetry feed
│   ├── pumpfun.py                    # Pump.fun WebSocket feed & pool detection
│   ├── wallet_intelligence.py        # Copy-trading & smart money tracker
│   ├── startup_checks.py             # Pre-flight environment & permission validation
│   ├── telegram_notify.py            # Async Telegram trade & health notifications
│   ├── telegram_commands.py          # Interactive Telegram control interface
│   └── disk_guard.py                 # Disk space & filesystem health monitor
├── dashboard/                        # Telemetry & Observability Dashboard
│   ├── app.py                        # Flask application factory & REST API endpoints
│   ├── templates/index.html          # Web dashboard interface
│   └── static/                       # Static assets (dashboard.js, CSS, icons, fonts)
├── data/                             # Persistent runtime state (excluded from Git)
│   ├── state.json                    # Active portfolio, cash, executions & equity curve
│   ├── heartbeat.json                # Watchdog heartbeat timestamp
│   ├── brain.json                    # Brain calibration history & checkpoints
│   ├── antirug_state.json            # Anti-Rug safety metrics & event logs
│   └── backups/                      # Automated atomic state snapshots
├── deploy/                           # Deployment Scripts & Container Health
│   ├── entrypoint.sh                 # Docker entrypoint with permission handling
│   ├── healthcheck.py                # Container healthcheck & deadlock watchdog
│   └── install_vps.sh                # Ubuntu 24.04 LTS native systemd installer
├── systemd/                          # Systemd Service Templates
│   ├── aeon-bot.service.template     # Trading bot systemd service
│   └── aeon-dashboard.service.template # Dashboard systemd service
├── tests/                            # Automated Regression & Audit Test Suite
│   ├── test_aeon_accounting_precision.py # Decimal & balance precision tests
│   ├── test_aeon_dashboard_alignment.py  # End-to-end dashboard telemetry tests
│   ├── test_aeon_deep_audit.py           # Risk integrity, sizing & limits tests
│   ├── test_aeon_staging_regression.py   # State recovery & backward compatibility
│   └── test_commands.py                  # Interactive Telegram command tests
├── scripts/                          # Operational & Validation Scripts
│   ├── backup.sh / restore.sh        # Disaster recovery & snapshot restoration
│   ├── run_tests.sh                  # Comprehensive test runner
│   └── test_five_audit_fixes.py      # Storage & risk regression suite
├── Dockerfile                        # Multi-stage container build (Python 3.12)
├── docker-compose.yml                # Production Compose configuration
├── requirements.txt                  # Pinned production dependencies
├── requirements-dev.txt              # Pinned test dependencies (pytest, pytest-timeout)
└── .env.example                      # Environment configuration template
```

---

## Core Subsystems

### 1. Risk Manager (`bot/risk.py`)
- **Survival Mode:** Enforces hard portfolio caps: max 15% position size, max 2 concurrent positions, strict cash constraints.
- **Drawdown Protection:** Halts entries if daily drawdown or consecutive loss limits are breached.
- **Exposure Ceilings:** Validates portfolio equity before every entry; rejects non-finite, negative, or boolean input corruptions.

### 2. Anti-Rug Security Engine (`bot/antirug.py`)
- **Fail-Closed Verification:** Operates in `ACTIVE` mode by default. Rejects tokens failing liquidity, mint authority, freeze authority, or top-10 concentration checks.
- **Mode Disambiguation:**
  - `ACTIVE`: Blocks hazardous tokens before entry order generation.
  - `SHADOW`: Logs potential hazards without blocking execution.
  - `OFF`: Disabled.

### 3. Adaptive Intelligence Brain (`bot/cervello.py`)
- **Walk-Forward Validation:** Analyzes closed trade batches across rolling windows (e.g., 20 trades).
- **Safe Parameter Bounds:** Only tunes parameters within hard-coded conservative clamps.
- **Outcome Classification:** Classifies parameter modifications strictly into `ACCEPTED`, `REJECTED`, or `UNKNOWN`. In-flight trials remain `PENDING`.

### 4. Telemetry Dashboard (`dashboard/app.py` & `dashboard.js`)
- **Strict Accounting Alignment:** Displays canonical portfolio equity, cash, invested capital, fees, and realized/unrealized P&L without client-side fallback fabrication.
- **Provider Telemetry:** Evaluates real network health for GeckoTerminal, DexScreener, GMGN, and Pump.fun from actual HTTP/WS telemetry rather than bot loop speed.
- **RPC Freshness:** Verifies timestamp freshness of RPC responses ($<60$s healthy, $60\text{--}180$s degraded, $>180$s down).

---

## Getting Started

### Prerequisites
- Python 3.12 or higher
- Linux (Ubuntu 24.04 LTS recommended) or Windows (development)
- Optional: Docker 24+ & Docker Compose v2

### Quick Setup (Local Development)

1. **Clone and create virtual environment:**
   ```bash
   git clone <repo-url> aeon
   cd aeon
   python3 -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt -r requirements-dev.txt
   ```

3. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env to set DASHBOARD_PASSWORD and Telegram credentials
   ```

4. **Run test suite:**
   ```bash
   pytest tests -v
   python test_tp_core.py
   python test_tp.py
   ```

5. **Start simulation bot:**
   ```bash
   python bot/paper_bot.py
   ```

6. **Start dashboard:**
   ```bash
   waitress-serve --host=127.0.0.1 --port=8080 --call dashboard.app:create_app
   ```

---

## VPS Deployment

AEON supports two robust deployment architectures on Ubuntu 24.04 LTS:

### Option A: Native Systemd (Recommended for Hetzner / Small VPS)

1. Unpack repository to `/opt/aeon-by-rekt`:
   ```bash
   sudo bash install.sh
   ```
2. Configure `/opt/aeon-by-rekt/.env`:
   ```bash
   sudo nano /opt/aeon-by-rekt/.env
   ```
3. Enable and start services:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now aeon-bot.service aeon-dashboard.service
   ```
4. Verify status:
   ```bash
   sudo systemctl status aeon-bot.service aeon-dashboard.service
   journalctl -u aeon-bot.service -f
   ```

### Option B: Docker Compose

1. Build and run containers:
   ```bash
   docker compose up -d --build
   ```
2. For automatic HTTPS with Caddy:
   ```bash
   docker compose --profile https up -d --build
   ```
3. For Cloudflare Tunnel (no open ports):
   ```bash
   docker compose --profile tunnel up -d --build
   ```

---

## Health Checks & Monitoring

- **Dashboard Health:** `GET http://127.0.0.1:8080/healthz` (returns HTTP 200 and operational JSON status).
- **Bot Heartbeat:** Updated every loop cycle in `data/heartbeat.json`. Monitored by `deploy/healthcheck.py` with automated deadlock mitigation.
- **Telegram Watchdog:** Real-time push alerts for entries, exits, risk breaches, and daily performance summaries.

---

## Backup & Disaster Recovery

- **Automated Snapshots:** Created every hour in `data/backups/`.
- **Manual Backup:**
  ```bash
  bash scripts/backup.sh
  ```
- **Restore Snapshot:**
  ```bash
  bash scripts/restore.sh data/backups/snapshot-YYYYMMDD-HHMMSS.tar.gz
  ```

---

## Quality Assurance & Testing

AEON enforces a strict regression-testing policy. All 69 automated tests and standalone verification suites must pass before any deployment:

```bash
# Run full PyTest test suite
pytest tests -v

# Run Take-Profit & Stop Execution Tests
python test_tp_core.py
python test_tp.py

# Run Full End-to-End Suite
bash scripts/run_tests.sh
```
