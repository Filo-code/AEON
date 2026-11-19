# FASE 1 — Inventario e audit (codice verificato riga per riga, versione v3 del 13/11)

## Inventario funzionalità esistenti (da conservare)
| Area | Funzionalità |
|---|---|
| Strategia | Entrate `volume_spike` (GeckoTerminal new_pools + DexScreener, moltiplicatore volume, età coppia, buy/sell ratio) e `copy_top_traders` (lista Vybe top 1000, match con holder RugCheck + buyer GeckoTerminal, minimo 5 wallet) |
| Filtri | dev %, top10 %, RugCheck score/rugged, acquisto balena, conferma Vybe opzionale |
| Uscite volume | stop −12 %, target +30 %, trailing 10 % dopo +15 %, max 15 min, vendite fallite simulate sui crolli |
| Uscite copia | segue i saldi on-chain dei top trader (RPC pubblico, fallback RugCheck), esce quando >50 % ha venduto ≥50 %; stop −25 %, trailing 20 % dopo +30 %, target +100 %, max 180 min; nessun blocco di ricompra |
| Esecuzione | quote Jupiter `/swap/v1/quote` (+ `/swap/v2/order` di riserva), cache 10 s, 1 retry su 429, fallback modello liquidità ("stimato") |
| Ricompra | cooldown 30 min solo `volume_spike` |
| Sopravvivenza | 100 € (cambio EUR/USD bloccato), size 15 % equity, minimo 3 €, MORTO a cassa < 3 € + commissioni senza posizioni |
| Cervello | ogni 10 trade, decision stump, 1 filtro entrata + 1 uscita per analisi entro BOUNDS, persistenza `brain.json` |
| Telegram | notifiche compra/vendi/fallita/start/stop/watchdog/giornaliero/MORTO/cervello; comandi /start /aiuto /stato /saldo /posizioni /trade /oggi /pausa /riprendi /chiudi /chiuditutto /config /imposta /cervello /crediti /dashboard |
| Dashboard | Flask + waitress, basic auth obbligatoria, brute-force 10/10 min, `/api/state`, `/healthz`, look identico al video |
| Deploy | Dockerfile, compose (bot, dashboard, profili `https` Caddy e `tunnel` cloudflared), systemd, README_VPS.md |
| Robustezza | deadline HTTP totale per tentativo, watchdog 240 s → exit 3, stato atomico, ripresa posizioni |

## Test esistenti (baseline eseguita il 13/11 07:06, tutti exit 0)
`test_cervello.py`, `test_survival.py`, `test_upgrades.py`, `test_commands.py`, `test_telegram.py`, `test_robustness.py` (6 script, ~53 s; usano la rete reale solo per il cambio EUR/USD).

## Problemi trovati, per gravità

### CRITICI
- **C1 – Perdita di Internet = liquidazione a zero.** `manage()`: se DexScreener non risponde (rete giù) e la posizione supera 2× max hold, viene chiusa con `no_price_data_assume_rug` a prezzo 1e‑18 (perdita 100 %). Dopo 30 min di blackout (volume) o 6 h (copia) tutte le posizioni vengono azzerate anche se il token sta benissimo.
- **C2 – Watchdog bloccabile da un lock.** Il thread watchdog chiama `save_state()` che prende `self.lock` (RLock). Se il ciclo principale è bloccato tenendo il lock, il watchdog resta appeso per sempre e il processo non si riavvia mai. Anche `logging.shutdown()` può bloccarsi sui lock degli handler.
- **C3 – state.json corrotto = reset silenzioso a 100 €.** `_load_state()` rinomina il file in `.bad` e riparte da zero: si perdono saldo, posizioni aperte e storico senza tentare una copia di riserva (non esiste alcun backup).
- **C4 – Dashboard esposta in HTTP pubblico** di default (`DASHBOARD_BIND=0.0.0.0`, systemd `--host=0.0.0.0`): password basic-auth in chiaro su Internet.

### ALTI
- **A1 – Nessun lock di istanza unica:** due bot (es. systemd + Docker) sullo stesso `data/` si sovrascrivono lo stato.
- **A2 – Scritture concorrenti sullo stesso `state.json.tmp`:** main thread, thread comandi (`run_actions`) e watchdog usano lo stesso nome temporaneo → `FileNotFoundError` o file misto. Nessun fsync della directory.
- **A3 – Risposte malformate fanno crollare il ciclo:** `DexScreener.tokens` / `RugCheck.holders` / `whale_check` assumono tipi (`p.get` su stringhe, `t["block_timestamp"]`); un JSON inatteso solleva eccezione e interrompe l'intero passaggio di `discover()`/`manage()` (uscite non gestite in quel giro).
- **A4 – Deadline HTTP non globale:** il limite di 20 s vale per tentativo; spacing (fino a 10 s), `sleep(1)` tra tentativi e il retry 429 di Jupiter (3,5 s + nuova richiesta) si sommano. Nessun backoff esponenziale/jitter; 5xx, timeout, rete e JSON malformati trattati allo stesso modo.
- **A5 – SIGTERM lento:** durante `discover()` le chiamate HTTP non controllano `stop`; con molte chiamate in coda si superano i 30 s di `stop_grace_period` → SIGKILL senza salvataggio finale né messaggio di stop.
- **A6 – `flush()` di Telegram non attende il messaggio in invio** (controlla solo `q.empty()`): il messaggio di arresto/watchdog può andare perso. Coda solo in RAM: crash/riavvio = notifiche perse; nessuna conferma (`ok`/`message_id`), nessuna deduplicazione.
- **A7 – Slippage da quote chiamato "reale"** ("slippage reale Jupiter") – è una stima da quote, non un fill realizzato. Nessun controllo di freschezza quote / mid, nessun motivo del fallback salvato nello storico (solo "stimato").
- **A8 – Healthcheck Docker senza recovery:** `restart: unless-stopped` NON riavvia un container `unhealthy`; nessun autoheal/kill. La dashboard non ha healthcheck.

### MEDI
- **M1 – Offset Telegram scritto non atomico** e salvato dopo l'esecuzione dei comandi → al riavvio un comando mutante (/chiuditutto) può essere rieseguito; nessuna protezione da comandi ripetuti.
- **M2 – Comandi Telegram leggono dati del bot senza lock** (`equity_value()` itera `positions` mentre il main thread le modifica → `RuntimeError: dictionary changed size`).
- **M3 – Cervello si ferma dopo 1000 trade:** `due()` usa `len(records)` che è troncato a 1000 → `n - last_n` resta 0 per sempre.
- **M4 – Cervello: overfitting** (decide su 10 trade, nessuna validazione out-of-sample, nessun rollback); i parametri appresi sovrascrivono al riavvio quelli impostati dall'utente con /imposta (ordine di caricamento).
- **M5 – I/O eccessivo:** `state.json` (fino a ~2 MB con 2000 trade) riscritto ogni 5 s → decine di GB/giorno di scritture sull'SSD della VPS.
- **M6 – `telegram_outbox.log` cresce senza limite;** nessun controllo spazio disco; `self.equity`/`self.closed` crescono senza limite in RAM.
- **M7 – Messaggio di VENDITA fuorviante:** "Saldo/totale" calcolati solo sulla cassa libera; con posizioni aperte il totale sembra in perdita.
- **M8 – Dashboard: anti brute-force aggirabile** (si fida del primo valore di `X-Forwarded-For` da qualunque IP privato – in Docker tutti i client arrivano da 172.x); `FAILS` non thread-safe; `KeyError` su state parziale → HTTP 500; nessun header di sicurezza.
- **M9 – Messaggi Telegram > 4096 caratteri** (es. /cervello) rifiutati con 400 e persi.
- **M10 – Nessun risk manager:** nessun limite di esposizione, drawdown o perdita giornaliera (scelta voluta dall'utente il 12/11: regole "sopravvivenza").
- **M11 – Dipendenze non bloccate** (`requests>=2.31`, immagini `caddy:2`, `cloudflared:latest`): un rebuild può cambiare versione.

### BASSI
- **B1 –** prezzo anomalo in un tick (dato errato) può attivare take profit/stop; nessuna conferma.
- **B2 –** latenza segnale → esecuzione non misurata; quote non disponibili non tracciate per motivo.
- **B3 –** `/healthz` restituisce sempre 200; nessun allarme se la dashboard è giù.
- **B4 –** EUR/USD e Vybe usage scritti con lo stesso tmp; `brain.json` senza fsync.
- **B5 –** systemd senza `WatchdogSec`, `ProtectSystem=full` (non strict).
- **B6 –** stato `restarts` incrementato anche dopo arresti puliti; nessuna distinzione crash/arresto nelle notifiche.
