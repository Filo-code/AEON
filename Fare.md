# AEON — Full Project Intelligence, Architecture Modernization, Integration & Engineering Audit

You are taking ownership of the complete AEON repository.

Your mission is to deeply understand the existing project, independently verify its architecture, research every relevant external service and technology, identify better alternatives where justified, improve the system architecture, integrate valuable capabilities, perform a comprehensive code audit, fix verified bugs, and prepare the entire project for production-grade PAPER TRADING deployment on **Ubuntu 26.04.1 LTS**.

This is not a request to blindly add libraries, APIs, services, indicators, scripts, or dashboard features.

Your responsibility is to build a coherent, efficient, maintainable, observable and reliable trading research system.

Every component must have a clear purpose, measurable value, defined resource cost, and documented failure behavior.

**Do not assume anything. Verify everything.**

---

# 1. NON-NEGOTIABLE ENGINEERING PRINCIPLES

Follow these principles throughout the entire project:

1. Never assume existing code is correct simply because it exists.
2. Never assume an audit report accurately describes the current implementation.
3. Never assume a feature is operational because its module exists.
4. Never assume an API endpoint, subscription, credential, or integration is available.
5. Never assume a provider's data is accurate, complete, fresh, or trustworthy.
6. Never assume a missing value means zero.
7. Never silently substitute estimated data for verified data.
8. Never implement a feature without understanding its effect on the rest of the architecture.
9. Never add an external dependency without explaining why it is needed.
10. Never introduce unnecessary API calls, background processes, polling loops, or redundant data sources.
11. Never remove existing functionality without documenting and proving that its removal is justified.
12. Never overwrite historical trading data.
13. Never silently discard errors, events, transactions, or execution attempts.
14. Never use hardcoded assumptions where runtime validation is possible.
15. Never claim a feature works without appropriate tests.

Use explicit data-quality classifications:

- VERIFIED
- ESTIMATED
- STALE
- UNKNOWN
- INVALID

All important decisions must preserve the source and quality of the information used.

If something is unclear, investigate it. If it cannot be verified, document the uncertainty rather than inventing an answer.

# 2. FIRST PHASE: REPOSITORY DISCOVERY AND FORENSIC AUDIT

Before modifying code, inspect the entire repository.

Read:

- README.md
- CLAUDE.md and AGENTS.md if present
- All architecture documentation
- All audit reports
- All deployment scripts
- All configuration files
- Docker and systemd files
- All Python modules
- All test files
- Existing backups and alternative implementations
- Existing data structures and historical records

Inspect every subsystem, including:

- paper_bot.py
- risk.py
- antirug.py
- cervello.py
- birdeye.py
- gmgn.py
- gmgn_adapter.py
- gmgn_agent.py
- pumpfun.py
- wallet_intelligence.py
- rpc_manager.py
- net.py
- storage.py
- profiles.py
- reset_manager.py
- startup_checks.py
- disk_guard.py
- telegram_commands.py
- telegram_notify.py
- screenshot.py
- dashboard/app.py
- dashboard templates and frontend assets

Also inspect every other file that affects runtime behavior.

## 2.1 Establish the actual architecture

Create an architecture map identifying:

- Every process.
- Every module.
- Every external service.
- Every API endpoint.
- Every WebSocket connection.
- Every background thread.
- Every scheduled task.
- Every persistent storage location.
- Every inter-module dependency.
- Every data flow.
- Every execution and recovery path.

Identify which integrations are genuinely active, which are optional, which are partially implemented, and which are unused.

Do not confuse documented intentions with working implementations.

## 2.2 Investigate historical versions

The repository may contain several backup versions of important modules.

Compare them carefully.

Determine:

- Which implementation is currently authoritative.
- Which fixes were introduced in previous versions.
- Which changes were reverted.
- Which fixes are missing from the current implementation.
- Whether any backup contains a valid solution to a current bug.

File timestamps alone are not proof that an implementation is correct.

Never overwrite original backup files during this investigation.

Produce:

- `docs/ARCHITECTURE_MAP.md`
- `docs/DEPENDENCY_INVENTORY.md`
- `docs/EXTERNAL_SERVICES.md`
- `docs/FORENSIC_AUDIT.md`
- `docs/KNOWN_ISSUES.md`

# 3. SECOND PHASE: RESEARCH EVERY EXTERNAL SERVICE

Independently research the official documentation of every service currently used or potentially useful.

Do not rely solely on existing comments or previous integration code.

Read the latest relevant documentation, API references, authentication requirements, WebSocket guides, rate limits, pricing, quotas, deprecation notices, terms of service, and practical integration examples.

The following services must be investigated.

## 3.1 Market discovery

- Pump.fun
- GMGN
- DexScreener
- GeckoTerminal

Investigate:

- Token discovery.
- New pool detection.
- Token migrations.
- Pair creation.
- Volume and liquidity.
- Market capitalization.
- Price changes.
- Historical candles.
- Real-time events.
- API availability.
- Rate limits.
- Authentication.
- WebSocket alternatives.

For GMGN, investigate the existing HTTP 403 / Cloudflare restriction.

Do not attempt to bypass access restrictions through browser impersonation, CAPTCHA evasion, proxy rotation, or unauthorized scraping.

Find a legitimate alternative if the service cannot be accessed reliably.

## 3.2 Blockchain infrastructure

Investigate:

- Solana RPC.
- Helius.
- Chainstack.
- Public RPC endpoints.
- Yellowstone gRPC.
- WebSocket subscriptions.

Compare:

- Latency.
- Reliability.
- Request limits.
- Connection limits.
- Historical access.
- Transaction subscriptions.
- Account subscriptions.
- Provider failover.
- Pricing.
- Free tiers.

Evaluate whether a primary RPC and one or more fallback providers are sufficient.

Do not create several competing data streams that deliver identical information.

## 3.3 Pricing and market intelligence

Investigate:

- Jupiter.
- Birdeye.
- Vybe Network.
- Bitquery.
- RugCheck.

Evaluate what each service contributes that the others do not.

In particular:

**Jupiter**
- Quote APIs.
- Route selection.
- Price impact.
- Slippage.
- Quote validity.
- Partial exits.
- API version migrations.
- Execution and transaction simulation capabilities.

Verify the current Swap API version before implementing a migration.

**Birdeye**
- Token information.
- Holder concentration.
- Liquidity.
- Token security.
- Historical information.
- Credit consumption.
- WebSocket availability.

**Vybe**
- Wallet activity.
- DEX trades.
- Trader analytics.
- WebSocket filters.
- Cost per connection and data consumption.

**Bitquery**
- GraphQL.
- Solana DEX trades.
- Streaming subscriptions.
- Historical data.
- Wallet intelligence.
- FOMO-specific transaction identification.

**RugCheck**
- Token reports.
- Risk scores.
- Authorities.
- Liquidity.
- Holder analysis.
- API availability and limitations.

Do not pay for two services that provide the same information unless the second one provides a demonstrable reliability or quality advantage.

## 3.4 Communications and infrastructure

Investigate official documentation for:

- Telegram Bot API.
- Flask.
- Waitress.
- Docker.
- Docker Compose.
- Caddy.
- Cloudflare Tunnel.
- Chromium headless.
- Python runtime and Linux process management.

Determine whether every current component is necessary.

The dashboard must not interfere with the trading engine.

# 4. THIRD PHASE: FOMO ON-CHAIN MONITOR

This is an important new capability.

Investigate the FOMO social trading platform and its observable Solana on-chain activity.

Start with these research references:

- https://docs.bitquery.io/docs/blockchain/Solana/fomo-api/
- https://github.com/chainstacklabs/fomo-onchain-listeners
- https://docs.vybenetwork.com/
- https://docs.helius.dev/
- https://solana.com/docs/rpc

The Chainstack implementation is an experimental reference, not production-ready software. Study its architecture and adapt only validated components.

Do not assume FOMO has a public API.

Do not rely on scraping private application endpoints, session cookies, browser internals, or undocumented interfaces.

Respect the platform's terms and applicable data-access restrictions.

## 4.1 FOMO monitoring objectives

Create an independent subsystem:

`bot/fomo_monitor.py`

Its initial purpose is PAPER-ONLY intelligence.

It should investigate how to:

- Identify transactions originating from FOMO.
- Identify the actual trader wallet.
- Distinguish app-level transaction signing from user wallet ownership.
- Identify genuine buys and sells.
- Identify the token involved.
- Determine trade size and direction.
- Record timestamps and transaction signatures.
- Track trader activity.
- Detect repeated purchases.
- Detect several monitored wallets buying the same token.
- Detect coordinated accumulation patterns.
- Detect sudden selling activity.
- Measure time between trader activity and token price movements.

Never assume that the same signer represents the user.

Validate the FOMO transaction identification method continuously.

If known signer addresses, fee accounts, or protocol identifiers are used, verify that they remain valid before trusting incoming data.

## 4.2 FOMO intelligence

Develop a wallet intelligence layer.

Each monitored wallet should have a historical profile containing:

- Total observed trades.
- Realized PnL where reliably calculable.
- Win rate.
- Average winning trade.
- Average losing trade.
- Median holding time.
- Average entry size.
- Historical performance by market-cap range.
- Token preferences.
- Repeated early entries.
- Performance after other wallets enter.
- Selling behavior.
- Reliability of the available historical data.

Do not rank wallets using a single metric.

Avoid rewarding wallets with high activity but poor risk-adjusted performance.

Distinguish observed performance from estimated performance.

## 4.3 FOMO signal generation

Create a separate signal engine.

Potential signals include:

- Multiple independent tracked wallets buying the same token.
- Increasing purchase frequency.
- Increasing aggregate purchase size.
- Short-interval wallet convergence.
- High-quality wallet participation.
- Accumulation followed by market confirmation.
- Distribution or coordinated selling.

A single wallet purchase must never automatically trigger an entry.

FOMO signals must pass through the normal AEON decision pipeline:

Discovery → Initial Filters → FOMO Intelligence → Anti-Rug → Risk Manager → Entry Validation.

FOMO must not bypass risk limits or security controls.

Initially, it must only generate PAPER signals and analytical records.

# 5. FOURTH PHASE: INTELLIGENT PROVIDER ORCHESTRATION

This is one of the most important architectural improvements.

I do not want every provider called for every token.

I do not want every integration constantly polling.

I do not want duplicate data requests.

I do not want a new service consuming resources without contributing useful information.

Create a central provider orchestration and API budget management layer.

Possible module:

`bot/provider_manager.py`

Evaluate whether it should be separated into smaller modules based on actual architectural needs.

## 5.1 Hierarchical information pipeline

Implement progressive information gathering.

### Stage A — Cheap discovery

Use the lowest-cost available event sources.

Collect only essential information:

- Mint.
- Symbol.
- Pool.
- Initial price.
- Basic liquidity.
- Creation timestamp.
- Source.

Reject obviously unsuitable candidates immediately.

### Stage B — Candidate validation

Only promising candidates receive additional requests.

Collect:

- More reliable liquidity.
- Volume.
- Market capitalization.
- Pair age.
- Price changes.
- Basic holder information.
- Trading activity.

Reuse existing cached information whenever possible.

### Stage C — Security investigation

Only candidates that pass earlier filters should receive deeper security analysis.

Use appropriate sources for:

- Mint authority.
- Freeze authority.
- Holder concentration.
- Creator concentration.
- Liquidity stability.
- Sellability.
- Suspicious activity.
- Data quality.

### Stage D — Intelligence enrichment

Use additional services only when their information can change the decision.

Examples:

- Wallet Intelligence.
- FOMO Monitor.
- Birdeye.
- Vybe.
- Additional blockchain queries.

### Stage E — Entry authorization

Before opening a paper position:

- Revalidate market data freshness.
- Verify risk limits.
- Verify liquidity.
- Validate the quote.
- Check execution assumptions.
- Record the complete decision.

### Stage F — Open position management

Open positions receive higher monitoring priority than new candidates.

Use event-driven updates whenever possible.

Request additional information when:

- Price approaches a critical level.
- TP1 is approaching.
- TP2 is approaching.
- Trailing stop conditions change.
- Liquidity deteriorates.
- A tracked wallet starts selling.
- An important risk signal appears.

Do not request every provider's data at every price tick.

### Stage G — Inactive and rejected candidates

Reduce monitoring intensity.

Do not keep expensive subscriptions or repeated requests alive for candidates that no longer matter.

Preserve relevant historical information for later analysis.

## 5.2 API budget management

Create a central budget for every external provider.

Track:

- Requests per minute.
- Requests per hour.
- Requests per day.
- Monthly credits.
- WebSocket connection time.
- Reconnection costs.
- Data volume.
- Cache hit rate.
- Failed requests.
- 429 responses.
- Timeouts.
- Estimated monetary consumption.

Support configurable limits.

Prioritize requests according to their importance.

Suggested priority:

1. Existing position safety and exit intelligence.
2. Existing position market data.
3. Critical token validation.
4. High-quality entry candidates.
5. FOMO intelligence.
6. General discovery.
7. Background analytics.

Implement:

- TTL caching.
- Request deduplication.
- Batch requests.
- Exponential backoff with jitter.
- Circuit breakers.
- Provider health scoring.
- Cooldowns.
- Connection reuse.
- Adaptive polling.
- Request deadlines.
- Fallback providers.

Do not retry unchanged requests when the error indicates an invalid key, insufficient API permissions, or a permanently unavailable endpoint.

Never let a background discovery task starve the position-management loop.

## 5.3 Cost-aware decisions

A paid provider should only be introduced if its measurable contribution justifies its cost.

Before activating a paid service:

1. Estimate monthly request volume.
2. Estimate monthly data consumption.
3. Calculate expected costs.
4. Identify a free or cheaper alternative.
5. Explain what additional information it provides.
6. Define a measurable benefit.
7. Make activation configurable.

Do not enable paid subscriptions automatically.

Document the economic rationale for each paid integration.

# 6. FIFTH PHASE: COMPLETE TRADE DATA ARCHIVE

This requirement is mandatory.

Every logical trade must have its own dedicated directory.

Do not rely exclusively on trades.csv or state.json.

Recommended structure:

`data/trades/YYYY/MM/TRADE_ID/`

Each trade directory should contain:

- `trade.json`
- `metadata.json`
- `entry.json`
- `price_history.jsonl`
- `decisions.jsonl`
- `execution_events.jsonl`
- `partial_exits.jsonl`
- `exit.json`
- `risk_assessment.jsonl`
- `provider_responses.jsonl`
- `wallet_activity.jsonl`
- `summary.json`

Adapt the exact structure if a better design is justified.

## 6.1 Trade identification

Every trade must have a permanent unique trade ID.

The same ID must be used across all related records.

A trade consists of one complete position lifecycle, even when it has several partial exits.

Do not count each partial exit as a separate completed trade.

## 6.2 Entry information

Record:

- Token mint.
- Token metadata.
- DEX and pool.
- Entry timestamp.
- Entry price.
- Market capitalization.
- Liquidity.
- Volume.
- Price impact.
- Quote source.
- Quote age.
- Estimated slippage.
- Position size.
- Risk percentage.
- Strategy.
- Discovery source.
- FOMO signal if applicable.
- Anti-Rug results.
- Wallet intelligence.
- Brain score.
- Risk Manager decision.

## 6.3 Lifecycle information

Record every meaningful state transition.

Examples:

- Initial entry.
- TP1 triggered.
- Partial exit requested.
- Partial exit successful.
- Partial exit failed.
- Breakeven activated.
- Trailing stop adjusted.
- TP2 triggered.
- Stop loss activated.
- Adaptive holding decision.
- Liquidity deterioration.
- Wallet distribution detected.
- Recovery activated.
- Exit deferred.
- Final exit.

Record both the decision and its result.

## 6.4 Exit information

Record:

- Exit reason.
- Exit timestamp.
- Exit price.
- Sold quantity.
- Remaining quantity.
- Every partial realization.
- Cumulative realized PnL.
- Unrealized PnL at relevant checkpoints.
- Total fees.
- Simulated execution costs.
- Slippage.
- MFE.
- MAE.
- Holding duration.

Accounting must remain mathematically consistent across multiple partial exits.

## 6.5 Raw data and provenance

Where technically and legally permitted, preserve the original provider responses or the minimum necessary raw event payloads.

Every observation should identify:

- Provider.
- Endpoint or subscription.
- Timestamp received.
- Timestamp of the underlying event.
- Data quality.
- Cache status.
- Request ID.
- Error status if applicable.

Never store API secrets or private credentials in trade archives.

Use append-only event records where possible.

Prevent accidental historical modifications.

## 6.6 Rejected trades

Create a separate archive for rejected opportunities.

Record:

- Token.
- Discovery source.
- Rejection stage.
- Exact rejection reason.
- Available market data.
- Security results.
- Subsequent market performance.

This will allow AEON to measure missed opportunities and evaluate whether its filters are too restrictive.

## 6.7 Crash recovery

An interrupted trade archive must be recoverable.

Implement:

- Atomic writes.
- Event replay.
- Incomplete archive detection.
- Reconciliation against portfolio state.
- Consistency checks.
- Periodic backups.
- Integrity checks.

The archive must never become a source of duplicate PnL or duplicate positions.

Keep the existing CSV exports for convenient analysis, but make the canonical trade record unambiguous.

# 7. SIXTH PHASE: COMPREHENSIVE BUG AUDIT

Perform a complete code review.

Do not only review the modules mentioned in old reports.

Investigate every component.

Prioritize the following areas.

## 7.1 Trading accounting

Verify:

- Entry cost basis.
- Multiple partial exits.
- TP1 and TP2.
- Realized PnL.
- Remaining cost basis.
- Remaining token quantity.
- Fees.
- Floating-point precision.
- Portfolio equity.
- Cash balance.
- Daily PnL.
- Historical trade count.

Use Decimal or integer base-unit arithmetic where appropriate.

## 7.2 Position lifecycle

Verify:

- No position disappears after a partial exit.
- No duplicate position is created.
- No position is closed twice.
- Partial exits are correctly accumulated.
- TP levels are not triggered repeatedly.
- Trailing stops never move in an invalid direction.
- Breakeven calculations are correct.
- Restart recovery restores every open position.

Time must not automatically force-close a profitable position merely because a fixed holding period elapsed.

Time should be an evaluation input, not an unconditional expiration rule.

Holding decisions must consider momentum, liquidity, volatility, risk, and the exit framework.

## 7.3 API and network reliability

Investigate:

- Connection timeouts.
- Slow endpoints.
- 403 restrictions.
- 429 rate limits.
- Malformed JSON.
- Incomplete responses.
- Stale quotes.
- Invalid prices.
- Duplicate events.
- Reconnection loops.
- WebSocket backpressure.
- Provider outages.

An API outage must never produce fabricated execution success.

A missing price must never be interpreted as a valid price of zero.

## 7.4 Concurrency and persistence

Investigate:

- Race conditions.
- Thread safety.
- Atomic writes.
- Lock management.
- Multiple process execution.
- State corruption.
- Partial file writes.
- Disk exhaustion.
- Backup integrity.
- Shutdown behavior.
- Recovery after forced termination.

## 7.5 Risk and security

Verify:

- Maximum position sizing.
- Maximum portfolio exposure.
- Daily drawdown.
- Consecutive losses.
- Position count.
- Minimum liquidity.
- Risk pause.
- Unknown data handling.
- Anti-Rug fail-closed behavior.

No strategy or intelligence subsystem may bypass Risk Manager.

## 7.6 Brain and adaptive learning

Review the entire Cervello subsystem.

Verify:

- Statistical significance.
- Sample sizes.
- Out-of-sample evaluation.
- Walk-forward testing.
- Parameter bounds.
- Rollback.
- Failed experiment history.
- Data leakage.
- Survivorship bias.
- Look-ahead bias.

The Brain must never modify risk invariants.

It must not learn from incomplete or incorrectly attributed trades.

# 8. SEVENTH PHASE: DASHBOARD REDESIGN AND OBSERVABILITY

Do not create dashboard features simply to make the dashboard look more impressive.

Every dashboard element must answer a real operational or analytical question.

First inspect the existing dashboard and identify what is useful, misleading, redundant, or missing.

Build on the current technology unless a migration has a clear technical justification.

The dashboard should provide the following areas.

## 8.1 Command Center

- Current bot status.
- Portfolio equity.
- Available cash.
- Invested capital.
- Realized PnL.
- Unrealized PnL.
- Drawdown.
- Open positions.
- Recent trades.
- System health.

## 8.2 Open Positions

For every position:

- Token.
- Entry price.
- Current price.
- Current PnL.
- Holding duration.
- Liquidity.
- TP progress.
- Stop loss.
- Trailing status.
- MFE and MAE.
- Risk status.

## 8.3 Market Intelligence

- Discovery activity.
- Token candidates.
- Provider health.
- Market liquidity.
- FOMO activity.
- Wallet convergence.
- Anti-Rug decisions.
- Rejected candidates.

## 8.4 Trading Analytics

- Strategy performance.
- Win rate.
- Profit factor.
- Average win/loss.
- Expectancy.
- Maximum drawdown.
- Holding time distribution.
- Exit reason distribution.
- MFE versus realized profit.
- MAE versus realized loss.
- Performance by market-cap range.
- Performance by liquidity range.
- Performance by discovery source.

Avoid misleading statistics and clearly label insufficient samples.

## 8.5 Provider Monitoring

Display:

- Provider status.
- Last successful request.
- Last error.
- Latency.
- Request usage.
- Monthly consumption.
- Rate-limit utilization.
- Circuit-breaker state.
- Estimated cost.

## 8.6 Trade Explorer

Every completed and open trade should be accessible through its dedicated archive.

Provide a complete chronological lifecycle view.

The user must be able to understand exactly why AEON entered, held, partially exited, or closed a position.

## 8.7 System Health

- CPU.
- RAM.
- Disk.
- Process health.
- Thread health.
- WebSocket status.
- API connectivity.
- Storage integrity.
- Backup status.
- Recent exceptions.

The dashboard must be read-only with respect to critical trading state unless an explicitly authorized control action is implemented.

Secure it properly. Never expose it publicly without authentication and HTTPS or a properly configured private tunnel.

# 9. EIGHTH PHASE: TELEGRAM

Review the existing Telegram implementation.

Telegram should provide concise and actionable notifications.

Avoid flooding the user.

Implement notification priorities:

- Critical: position safety, failed recovery, system failure.
- High: entry, exit, important partial exit.
- Medium: significant risk or intelligence event.
- Low: periodic summaries.

Use aggregation and deduplication.

Commands must have appropriate authorization and validation.

Long reports should be available through the dashboard rather than sent as enormous Telegram messages.

# 10. NINTH PHASE: UBUNTU 26.04.1 LTS MIGRATION

The final system must be designed and tested for:

**Ubuntu 26.04.1 LTS.**

Do not assume the existing Ubuntu 24.04 installation scripts will work unchanged.

Inspect and validate:

- Python compatibility.
- Package availability.
- Docker Engine.
- Docker Compose plugin.
- Systemd.
- Filesystem behavior.
- File permissions.
- Linux locking.
- Chromium compatibility.
- Waitress.
- Flask.
- WebSocket dependencies.
- Network configuration.
- Time synchronization.
- Log rotation.

The current repository uses Python 3.12 and a Debian-based Docker image. Verify whether this remains an appropriate choice.

Choose a supported Python runtime compatible with every dependency.

Pin production dependencies.

Avoid unnecessary system packages.

Ensure services restart safely after VPS reboots.

Implement correct graceful shutdown and recovery.

Provide:

- A reproducible installation script.
- A health-check script.
- A backup script.
- A restore script.
- A migration guide.
- A systemd or Docker deployment strategy with clear justification.

Never assume that Ubuntu 26.04.1 package versions or availability match Ubuntu 24.04.

# 11. TENTH PHASE: TESTING AND VALIDATION

Build a serious test strategy.

The existing test suite must be preserved and improved.

Test:

- Risk boundaries.
- Accounting precision.
- Multiple partial exits.
- TP1 and TP2 transitions.
- Trailing stop.
- Adaptive holding.
- Missing price data.
- Jupiter timeouts.
- Invalid quotes.
- Provider failover.
- Duplicate market events.
- WebSocket reconnects.
- FOMO event parsing.
- FOMO wallet attribution.
- API budget enforcement.
- Rate-limit handling.
- State corruption.
- Restart recovery.
- Trade archive integrity.
- Dashboard accounting.
- Telegram authorization.
- Disk exhaustion.

Use mocks for external services.

Do not consume paid API credits unnecessarily during tests.

Introduce deterministic simulation scenarios.

Run regression tests after every significant change.

Never modify tests merely to make them pass if doing so hides a real defect.

If a test expectation is incorrect, document why and replace it with a mathematically and technically valid expectation.

# 12. ELEVENTH PHASE: PERFORMANCE AND RESOURCE EFFICIENCY

The system must run reliably on a modest VPS.

Optimize:

- CPU consumption.
- RAM consumption.
- Disk writes.
- Network traffic.
- API consumption.
- Thread count.
- WebSocket subscriptions.
- Cache utilization.
- Dashboard refresh frequency.

Avoid excessive browser processes.

Avoid unnecessary Chromium startup.

Do not introduce heavy infrastructure such as Redis, Kafka, PostgreSQL, or additional containers unless the actual workload justifies it.

Prefer the simplest reliable architecture.

If introducing a database, explain why existing storage is insufficient and provide a migration and recovery strategy.

# 13. TWELFTH PHASE: SECURITY

Review the entire project for:

- Exposed API keys.
- Telegram tokens.
- Passwords.
- Insecure environment handling.
- Unsafe file permissions.
- Unauthenticated endpoints.
- Public dashboard exposure.
- Unsafe subprocess execution.
- Dependency vulnerabilities.
- Unsafe deserialization.
- Log leakage.

Never print credentials into logs or reports.

Never commit secrets.

Do not put sensitive data in trade archives.

If an exposed credential is discovered, identify the affected variable without reproducing its value and recommend rotation.

# 14. IMPLEMENTATION ORDER

Do not attempt to implement everything simultaneously.

Follow this order.

**Priority 0 — Forensic discovery**

Understand the repository, identify the actual working implementation, establish the architecture and document existing problems.

**Priority 1 — Correctness and safety**

Fix verified accounting, risk, position lifecycle, persistence, concurrency and recovery defects.

**Priority 2 — Trade archive**

Implement complete per-trade storage, event tracking and historical integrity.

**Priority 3 — Provider orchestration**

Introduce centralized request budgets, caching, deduplication, fallback and adaptive monitoring.

**Priority 4 — Existing provider improvements**

Improve the existing integrations and remove redundant or unreliable behavior.

**Priority 5 — FOMO intelligence**

Implement the independent FOMO monitor, validate attribution and introduce PAPER signal generation.

**Priority 6 — Cervello and wallet intelligence**

Integrate better data and validated statistical evaluation.

**Priority 7 — Dashboard and Telegram**

Improve observability and operational controls.

**Priority 8 — Ubuntu 26.04.1 deployment**

Build and validate the complete deployment environment.

**Priority 9 — Final verification**

Run the complete regression suite, integration tests and simulated operational scenarios.

Do not prioritize visual features over accounting, risk, persistence, and reliability.

# 15. EXECUTION RULES

You are authorized to inspect and modify the repository code, configuration, tests, documentation and deployment scripts.

However:

- Do not connect to a live trading wallet.
- Do not execute real transactions.
- Do not introduce real-capital execution.
- Do not remove historical data.
- Do not overwrite original backup versions.
- Do not activate paid services without explicit approval.
- Do not silently change risk limits.
- Do not deploy to a production VPS without explicit approval.
- Do not invent credentials.
- Do not silently change the meaning of existing strategies.

AEON remains a PAPER TRADING system.

All new integrations must have independent configuration switches.

New features should initially be disabled or in SHADOW mode where appropriate.

Existing validated behavior must be preserved unless a documented defect requires correction.

# 16. DELIVERABLES

Produce the following documentation:

- `docs/ARCHITECTURE_MAP.md`
- `docs/DEPENDENCY_INVENTORY.md`
- `docs/EXTERNAL_SERVICES.md`
- `docs/API_COST_ANALYSIS.md`
- `docs/PROVIDER_ORCHESTRATION.md`
- `docs/FOMO_RESEARCH.md`
- `docs/TRADE_ARCHIVE_SPECIFICATION.md`
- `docs/BUG_AUDIT.md`
- `docs/SECURITY_AUDIT.md`
- `docs/UBUNTU_26_04_MIGRATION.md`
- `docs/TEST_STRATEGY.md`
- `docs/IMPLEMENTATION_LOG.md`
- `docs/FINAL_VERIFICATION.md`

Every external service must have:

- Official documentation URL.
- Actual implemented use case.
- Authentication requirements.
- Rate limits.
- Pricing.
- Free-tier limitations.
- Fallback strategy.
- Failure behavior.
- Activation configuration.

Every bug fix must have:

- Description.
- Root cause.
- Files modified.
- Why the fix is correct.
- Regression test.
- Test result.

Every new feature must have:

- Purpose.
- Expected benefit.
- Dependencies.
- API cost.
- Resource cost.
- Failure behavior.
- Configuration.
- Tests.

# 17. FINAL ACCEPTANCE CRITERIA

The project is not complete simply because it starts.

It is complete only when:

1. The architecture is understood and documented.
2. Every important external integration is verified.
3. Provider requests are centrally managed.
4. Position management cannot be starved by discovery.
5. API usage is measured and controlled.
6. Every logical trade has its own complete archive.
7. Partial exits are mathematically consistent.
8. Crash recovery is tested.
9. No critical accounting or risk regression remains.
10. FOMO monitoring works independently and produces validated PAPER signals.
11. Dashboard figures match canonical portfolio accounting.
12. Telegram does not generate unnecessary notification spam.
13. Deployment works on Ubuntu 26.04.1.
14. Tests cover both normal behavior and failure scenarios.
15. All external integrations can be disabled independently.
16. No undocumented or unnecessary dependencies have been introduced.

At the end, provide a clear report separating:

- Completed work.
- Verified fixes.
- New integrations.
- Optional integrations.
- Features requiring credentials.
- Features requiring paid subscriptions.
- Known limitations.
- Remaining bugs.
- Tests passed and failed.
- Deployment readiness.

# 18. HOW TO WORK

Work autonomously and systematically.

Do not stop after writing an audit report. Continue with implementation, tests, documentation and verification.

However, do not make assumptions to maintain momentum.

When an important external dependency, architectural decision, paid service, security-sensitive change or irreversible data migration requires a decision, document the alternatives and ask for approval.

For routine engineering decisions, use the simplest technically sound solution and document the rationale.

Maintain a persistent implementation log so work can resume without losing context.

Never claim completion for untested functionality.

Your ultimate objective is not to make AEON bigger.

Your objective is to make AEON **more intelligent, more selective, more reliable, more efficient, more transparent and mathematically correct**.

Every component, from the smallest helper script to the main dashboard, must earn its place in the system.

Start with the complete forensic audit. Understand the entire repository before making the first architectural change.