"""'Cervello' — a small, transparent learning module for the PAPER bot (v5: dynamic frequency & enriched telemetry).

After every closed trade it stores the entry features, execution telemetry, and outcome.
Dynamic analysis frequency:
  - Up to 50 closed trades: analyses every 10 trades (at 10, 20, 30, 40, 50).
  - From 50 to 100+ closed trades: analyses every 50 trades on a 50-trade rolling window
    to prevent overfitting on short-term market noise.
MODIFYING a parameter in `validated` mode is a separate, stricter step:
  1. observation mode: no change before `observe_trades` closed trades (analysis only);
  2. at least `min_samples` trades in the window and `min_group` on each side of a pattern;
  3. the pattern is found on the TRAINING part (older trades) and must be confirmed OUT-OF-SAMPLE on the
     most recent `oos_frac` of the window, with a Welch t-statistic >= `min_t` on the training part;
  4. ONE parameter per analysis, one step at a time, inside hard bounds (BOUNDS);
  5. every change is evaluated on the next `eval_trades` trades: if the average result is worse than the
     baseline by more than `rollback_margin_pct` points it is ROLLED BACK automatically;
  6. history of every change (applied / kept / rolled back) in data/brain.json.
It can only touch the strategy parameters in BOUNDS: never risk limits (risk.*), position size, or values
the user set with /imposta. No wallet and no real order exist anywhere in this bot.
"""
import logging, math, os, time
from datetime import datetime

from storage import atomic_json, load_json_safe

log = logging.getLogger("paperbot.cervello")

NUM = {  # feature -> Italian label, unit
    "buy_slip_pct": ("lo slippage d'acquisto", "%"), "liquidity": ("la liquidità del pool", " $"),
    "vol_ratio": ("il moltiplicatore di volume", "×"), "age_min": ("l'età della coppia", " min"),
    "vol_m5": ("il volume a 5 minuti", " $"), "top10_pct": ("la quota dei top 10 holder", "%"),
    "dev_pct": ("la quota del dev", "%"), "whale_buy_usd": ("l'acquisto della balena", " $"),
    "top_traders_in": ("i top trader presenti", ""),
}
CAT = {"dex": "il DEX", "entry_type": "il tipo di entrata", "fascia": "la fascia oraria"}

# Parameters the brain may touch: (min, max, max step per change). Bounds are absolute safety rails.
BOUNDS = {
    "min_liquidity_usd": (0, 50000, 10000), "spike_multiplier": (4, 20, 2), "max_pair_age_minutes": (10, 120, 15),
    "max_top10_pct": (8, 35, 4), "min_volume_5m_usd": (1000, 25000, 3000), "whale_buy_usd": (100, 3000, 300),
    "max_dev_holding_pct": (0.5, 6, 1), "min_top_traders_in": (3, 12, 1),
    "take_profit_pct": (0.15, 0.80, 0.05), "stop_loss_pct": (0.06, 0.25, 0.02),
    # max BUY slippage accepted at entry (Jupiter quote or model estimate), in %.
    "max_entry_slippage_pct": (3.0, 60.0, 10.0),
}
NULLABLE = {"max_entry_slippage_pct"}          # None = no limit; the first value is set directly from the data
# "loser above threshold" -> lower this max-param ; "loser below threshold" -> raise this min-param
ENTRY_RULES = {
    ("buy_slip_pct", "above"): ("min_liquidity_usd", "raise_liq"),
    ("liquidity", "below"): ("min_liquidity_usd", "to_threshold"),
    ("vol_ratio", "below"): ("spike_multiplier", "to_threshold"),
    ("age_min", "above"): ("max_pair_age_minutes", "to_threshold"),
    ("top10_pct", "above"): ("max_top10_pct", "to_threshold"),
    ("vol_m5", "below"): ("min_volume_5m_usd", "to_threshold"),
    ("whale_buy_usd", "below"): ("whale_buy_usd", "to_threshold"),
    ("dev_pct", "above"): ("max_dev_holding_pct", "to_threshold"),
    ("top_traders_in", "below"): ("min_top_traders_in", "to_threshold"),
}
PNAME = {"min_liquidity_usd": "la liquidità minima", "spike_multiplier": "il moltiplicatore di volume minimo",
         "max_pair_age_minutes": "l'età massima della coppia", "max_top10_pct": "la quota massima dei top 10",
         "min_volume_5m_usd": "il volume minimo a 5 min", "whale_buy_usd": "la balena minima",
         "max_dev_holding_pct": "la quota massima del dev", "min_top_traders_in": "i top trader minimi",
         "max_hold_minutes": "la durata massima", "take_profit_pct": "il take profit", "stop_loss_pct": "lo stop loss",
         "max_entry_slippage_pct": "lo slippage massimo d'entrata"}


def fascia(h):
    return "notte (0-6)" if h < 6 else "mattina (6-12)" if h < 12 else "pomeriggio (12-18)" if h < 18 else "sera (18-24)"


def fmt(v, unit=""):
    if unit == " $":
        return f"{v:,.0f} $".replace(",", ".")
    if isinstance(v, float) and v < 1 and unit == "":
        return f"{v:.2f}"
    return (f"{v:.1f}" if isinstance(v, float) and v % 1 else f"{v:.0f}").replace(".", ",") + unit


def fmt_param(k, v):
    if k == "max_entry_slippage_pct":
        return "nessun limite" if v is None else fmt(float(v), "%")
    if k in ("take_profit_pct", "stop_loss_pct"):
        return f"{100 * v:.0f}%"
    if k in ("min_liquidity_usd", "min_volume_5m_usd", "whale_buy_usd"):
        return fmt(v, " $")
    if k in ("max_pair_age_minutes", "max_hold_minutes"):
        return f"{v:.0f} min"
    if k in ("max_top10_pct", "max_dev_holding_pct"):
        return fmt(float(v), "%")
    if k == "spike_multiplier":
        return f"{v:g}×"
    return f"{v:g}"


# Never learnable, whatever happens (risk manager + sizing belong to the user / risk module)
FORBIDDEN = {"position_size_pct", "max_open_positions", "risk", "survival", "starting_balance_eur",
             # safety controls: user-configured, NEVER touched by learning (AntiRug thresholds, price integrity,
             # trade guards, risk profile). Enforced here and by tests.
             "antirug", "price_integrity", "trade_guards", "risk_profile", "disk_guard",
             "preventive_max_entry_slippage_pct", "blacklist",
             "max_hold_minutes", "max_exit_slippage_pct", "critical_exit_slippage_pct", "exit_slippage"}
assert not (set(BOUNDS) & FORBIDDEN)
assert not any(k.startswith(("antirug", "price_integrity", "trade_guards")) for k in BOUNDS)
SAFETY_ONLY_TIGHTEN = {"max_entry_slippage_pct"}    # once set it can move, but never back to "no limit"

SEED_SCHEMA = {"params": dict, "changes": list}


def validate_seed(seed):
    """(valid_params, problems). Schema + bounds + type check of data_seed/brain_rules.json. Unknown keys, values out of
    the safety rails and non-numbers are dropped (reported), never clamped silently."""
    probs, ok = [], {}
    if not isinstance(seed, dict):
        return {}, ["seed non è un oggetto JSON"]
    for k, t in SEED_SCHEMA.items():
        if not isinstance(seed.get(k), t):
            probs.append(f"campo '{k}' mancante o di tipo errato")
    for k, v in (seed.get("params") or {}).items() if isinstance(seed.get("params"), dict) else []:
        if k not in BOUNDS:
            probs.append(f"{k}: non è un parametro imparabile (ignorato)"); continue
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            probs.append(f"{k}: valore non numerico (ignorato)"); continue
        lo, hi, _ = BOUNDS[k]
        if not (lo <= v <= hi):
            probs.append(f"{k}={v}: fuori dai limiti {lo}–{hi} (ignorato)"); continue
        ok[k] = v
    for c in seed.get("changes") or [] if isinstance(seed.get("changes"), list) else []:
        if not (isinstance(c, dict) and c.get("param") in BOUNDS and "new" in c):
            probs.append("storico: voce non valida"); break
    for k, v in ok.items():                           # origin: the value must be the last logged change of that key
        last = [c for c in seed.get("changes") or [] if isinstance(c, dict) and c.get("param") == k]
        if not last or last[-1].get("new") != v:
            probs.append(f"{k}={v}: nessuna modifica registrata che lo spieghi (origine non verificabile)")
    return ok, probs


DEFAULTS = {
    "enabled": True,
    "adapt": True,
    "every_n_trades": 10,
    "every_n_trades_after_50": 50,
    "observe_trades": 30,
    "min_samples": 30,
    "window": 50,
    "min_group": 4,
    "min_gap_pct": 8,
    "oos_frac": 0.3,
    "min_t": 2.0,
    "min_group_oos": 2,
    "eval_trades": 15,
    "rollback_margin_pct": 3.0,
}


def compute_dynamic_interval(n, last_n, bc):
    """Scaled analysis cadence:
    - Up to 50 trades (and before the 50-trade analysis has run): every `every_n_trades` (default 10).
    - Once >= 50 trades have been analyzed (or n > 50): every `every_n_trades_after_50` (default 50).
    """
    base_step = max(1, int(bc.get("every_n_trades", 10)))
    scaled_step = max(base_step, int(bc.get("every_n_trades_after_50", 50)))
    if last_n >= 50:
        return scaled_step
    return base_step


def build_trade_record(pos, trade, tz, params_snapshot=None):
    """Build enriched brain telemetry record for brain.json."""
    i = pos.get("info") or {}
    ex_meta = pos.get("exec") or {}
    costs = pos.get("costs") or {}
    h = datetime.fromtimestamp(pos["entry_ts"], tz).hour
    r = {
        "ts": trade.get("exit_ts", time.time()),
        "token": trade["token"],
        "mint": pos.get("mint"),
        "entry_type": pos["entry_type"],
        "dex": (pos.get("dex") or "?").lower(),
        "fascia": fascia(h),
        "hour": h,
        "liquidity": float(pos.get("liquidity") or 0),
        "buy_slip_pct": trade["buy_slip_pct"],
        "sell_slip_pct": trade.get("sell_slip_pct", 0.0),
        "entry_expected_slippage_pct": trade.get("entry_expected_slippage_pct", trade["buy_slip_pct"]),
        "entry_realized_slippage_pct": trade.get("entry_realized_slippage_pct", trade["buy_slip_pct"]),
        "entry_slippage_drift_pct": trade.get("entry_slippage_drift_pct", 0.0),
        "buy_slip_src": trade.get("buy_slip_src", costs.get("buy_slip_src", "stimato")),
        "sell_slip_src": trade.get("sell_slip_src", "stimato"),
        "entry_quote_source": trade.get("entry_quote_source", costs.get("buy_quote_source")),
        "exit_quote_source": trade.get("exit_quote_source"),
        "buy_route": costs.get("buy_route"),
        "sell_route": trade.get("sell_route"),
        "signal_latency_s": trade.get("signal_latency_s", ex_meta.get("signal_latency_s")),
        "quote_latency_ms": trade.get("entry_quote_latency_ms", costs.get("buy_quote_latency_ms")),
        "exit_quote_latency_ms": trade.get("exit_quote_latency_ms"),
        "pnl_pct": trade["pnl_pct"],
        "pnl_usd": trade.get("pnl_usd", 0.0),
        "win": trade["pnl_usd"] > 0,
        "partial": bool(trade.get("partial", False)),
        "exit_reason": trade["exit_reason"],
        "price_move_pct": trade.get("price_move_pct"),
        "hold_min": trade["hold_sec"] / 60,
        "hold_bucket_min": (
            "0-15m" if (trade["hold_sec"] / 60) <= 15 else
            "15-30m" if (trade["hold_sec"] / 60) <= 30 else
            "30-60m" if (trade["hold_sec"] / 60) <= 60 else
            "60-120m" if (trade["hold_sec"] / 60) <= 120 else
            "120-180m" if (trade["hold_sec"] / 60) <= 180 else "180m+"
        ),
        "exit_expected_slippage_pct": trade.get("sell_slip_pct", 0.0),
        "exit_price_impact_pct": trade.get("sell_price_impact_pct"),
        "exit_quote_failures": trade.get("failed_exits", 0),
        "partial_exit_used": bool(trade.get("partial", False) or "_partial_slip_guard" in str(trade.get("exit_reason", ""))),
        "liquidity_change_pct": (
            round((trade["exit_liquidity"] / trade["entry_liquidity"] - 1.0) * 100.0, 2)
            if trade.get("entry_liquidity") and trade.get("exit_liquidity") else None
        ),
        "baseline_15m_pnl_pct": ((trade.get("adaptive_baseline_quote") or {}).get("pnl_pct")),
        "hold_vs_15m_edge_pct": (
            round(trade["pnl_pct"] - trade["adaptive_baseline_quote"]["pnl_pct"], 2)
            if isinstance((trade.get("adaptive_baseline_quote") or {}).get("pnl_pct"), (int, float)) else None
        ),
        "recovery_classification": trade.get("recovery_classification"),
        "anti_panic_triggered": bool(trade.get("anti_panic_triggered", False)),
    }
    if params_snapshot is not None:
        r["params"] = params_snapshot
    if i.get("copy_timeframes_matched") is not None:
        r["copy_timeframes_matched"] = i["copy_timeframes_matched"]
    if i.get("copy_trader_weight") is not None:
        r["copy_trader_weight"] = float(i["copy_trader_weight"])
    for k in ("vol_ratio", "age_min", "vol_m5", "top10_pct", "dev_pct", "whale_buy_usd", "top_traders_in"):
        if i.get(k) is not None:
            try:
                r[k] = float(i[k])
            except (TypeError, ValueError):
                pass
    return r


def welch_t(a, b):
    if len(a) < 2 or len(b) < 2:
        return 0.0
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    return abs(ma - mb) / se if se > 0 else (float("inf") if ma != mb else 0.0)


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


class ValidatedBrain:
    mode = "validated"

    def __init__(self, cfg, data_dir, tz=None, locked=None):
        self.c = cfg
        self.bc = {**DEFAULTS, **(cfg.get("brain") or {})}
        if "min_trades" in self.bc and "min_samples" not in (cfg.get("brain") or {}):
            self.bc["min_samples"] = self.bc["min_trades"]          # backward compatible key
        self.path = os.path.join(data_dir, "brain.json")
        self.tz = tz
        self.locked = set(locked or ())                              # keys set by the user via /imposta
        self.st = {"records": [], "analyses": [], "changes": [], "params": {}, "last_n": 0, "total_n": 0,
                   "diagnosis": "", "pending": None}
        obj, src = load_json_safe(self.path)
        if obj:
            self.st.update(obj)
            if not self.st.get("total_n"):
                self.st["total_n"] = len(self.st["records"])
        elif src == "corrupt":
            log.warning("brain.json unreadable: starting a fresh brain")
        self._orig = {k: cfg[k] for k in BOUNDS if cfg.get(k) is not None}   # config values (for loosening back)
        for k, v in self.st["params"].items():      # learned values survive restarts (unless user-locked)
            if k in BOUNDS and k not in self.locked and v is not None:
                lo, hi, _ = BOUNDS[k]
                cfg[k] = max(lo, min(hi, v))

    @property
    def enabled(self):
        return self.bc.get("enabled", True)

    def lock_param(self, key):
        """The user changed this key with /imposta: the brain will not touch it again."""
        self.locked.add(key)
        if self.st.get("pending") and self.st["pending"]["param"] == key:
            self.st["pending"] = None
        self.save()

    def save(self):
        try:
            atomic_json(self.path, self.st)
        except Exception as e:
            log.error("cannot save brain.json: %s", e)

    # ------------------------------------------------------------------ data
    def record(self, pos, trade):
        if not self.enabled or trade["exit_reason"] in ("end_of_run", "manual_close"):
            return
        r = build_trade_record(pos, trade, self.tz, params_snapshot={k: self.c.get(k) for k in BOUNDS})
        self.st["records"].append(r)
        self.st["records"] = self.st["records"][-1000:]
        self.st["total_n"] = self.st.get("total_n", 0) + 1
        self.save()

    def _analysis_interval(self, n):
        return compute_dynamic_interval(n, self.st.get("last_n", 0), self.bc)

    def due(self):
        n = self.st.get("total_n", len(self.st["records"]))
        interval = self._analysis_interval(n)
        min_start = min(self.bc.get("min_trades", 10), self.bc.get("min_samples", 30))
        if self.enabled and n >= min_start and n - self.st["last_n"] >= interval:
            return True
        if self.enabled and self.st.get("pending"):
            p = self.st["pending"]
            after = [r for r in self.st["records"] if r["ts"] > p["ts"]]
            if len(after) >= self.bc.get("eval_trades", 15):
                return True
        return False

    def phase(self):
        n = self.st.get("total_n", 0)
        if not self.enabled:
            return "spento"
        if not self.bc.get("adapt", True):
            return "solo analisi"
        if n < self.bc["observe_trades"]:
            return "osservazione"
        if self.st.get("pending"):
            return "verifica modifica"
        return "apprendimento"

    # ------------------------------------------------------------------ analysis primitives
    def _split(self, rows, feat, thr, bad):
        lo = [r for r in rows if feat in r and r[feat] <= thr]
        hi = [r for r in rows if feat in r and r[feat] > thr]
        return (hi, lo) if bad == "above" else (lo, hi)        # (bad group, good group)

    def _stumps(self, rows, mg):
        out = []
        for feat in NUM:
            vals = sorted({r[feat] for r in rows if feat in r})
            if len(vals) < 4:
                continue
            for q in (0.25, 0.4, 0.5, 0.6, 0.75):
                thr = vals[int(q * (len(vals) - 1))]
                lo = [r for r in rows if feat in r and r[feat] <= thr]
                hi = [r for r in rows if feat in r and r[feat] > thr]
                if len(lo) < mg or len(hi) < mg:
                    continue
                mlo, mhi = mean(r["pnl_pct"] for r in lo), mean(r["pnl_pct"] for r in hi)
                wlo, whi = mean(r["win"] for r in lo), mean(r["win"] for r in hi)
                bad = "above" if mhi < mlo else "below"
                out.append({"feat": feat, "thr": thr, "bad": bad, "gap": abs(mhi - mlo),
                            "bad_mean": min(mhi, mlo), "good_mean": max(mhi, mlo),
                            "bad_wr": whi if bad == "above" else wlo, "good_wr": wlo if bad == "above" else whi,
                            "n_bad": len(hi) if bad == "above" else len(lo), "n_good": len(lo) if bad == "above" else len(hi),
                            "t": welch_t([r["pnl_pct"] for r in lo], [r["pnl_pct"] for r in hi])})
        best = {}
        for s in out:   # best threshold per feature
            if s["feat"] not in best or s["gap"] > best[s["feat"]]["gap"]:
                best[s["feat"]] = s
        return sorted(best.values(), key=lambda s: -s["gap"])

    def _cats(self, rows, mg):
        out = []
        for feat in CAT:
            groups = {}
            for r in rows:
                groups.setdefault(r.get(feat, "?"), []).append(r)
            g = {k: v for k, v in groups.items() if len(v) >= mg}
            if len(g) < 2:
                continue
            means = {k: mean(r["pnl_pct"] for r in v) for k, v in g.items()}
            worst, bestk = min(means, key=means.get), max(means, key=means.get)
            out.append({"feat": feat, "worst": worst, "best": bestk, "worst_mean": means[worst],
                        "best_mean": means[bestk], "gap": means[bestk] - means[worst], "n": len(g[worst])})
        return sorted(out, key=lambda s: -s["gap"])

    def _oos_confirms(self, s, test):
        bad, good = self._split(test, s["feat"], s["thr"], s["bad"])
        k = self.bc["min_group_oos"]
        if len(bad) < k or len(good) < k:
            return False, f"fuori campione troppo pochi trade ({len(bad)}/{len(good)})"
        mb, mg_ = mean(r["pnl_pct"] for r in bad), mean(r["pnl_pct"] for r in good)
        need = self.bc["min_gap_pct"] / 2
        if mg_ - mb >= need and mb < 0:
            return True, f"confermato fuori campione: {mb:+.0f}% contro {mg_:+.0f}% su {len(bad) + len(good)} trade recenti"
        return False, f"NON confermato fuori campione ({mb:+.0f}% contro {mg_:+.0f}%)"

    def _change(self, key, new, reason, changes, baseline=None):
        if key not in BOUNDS or key in FORBIDDEN:
            return False
        if key in self.locked:
            log.info("BRAIN: %s is user-locked (/imposta), not changing it", key)
            return False
        lo, hi, step = BOUNDS[key]
        old = self.c.get(key)
        if key in NULLABLE:
            new = _slip_target(old, new)
        elif old is None:
            return False
        else:
            new = max(lo, min(hi, new))
            new = max(old - step, min(old + step, new))      # never jump more than one step
            new = round(new, 4) if isinstance(old, float) or key.endswith("_pct") else round(new)
            new = max(lo, min(hi, new))
        if old is not None and abs(new - old) < 1e-9:
            return False
        # Do not re-apply changes that previously failed and were rolled back (unless restoring original config)
        orig_map = getattr(self, "_orig", None) or self.bc.get("_orig", {})
        is_restoring_orig = (key in orig_map and orig_map[key] == new)
        if not is_restoring_orig and any(c.get("param") == key and c.get("new") == new and c.get("status") == "annullata"
               for c in self.st.get("changes", [])):
            log.info("BRAIN: %s -> %s was previously rolled back (failed experiment), skipping", key, new)
            return False
        self.c[key] = new
        self.st["params"][key] = new
        ch = {"ts": time.time(), "param": key, "old": old, "new": new, "reason": reason, "status": "in verifica",
              "at_n": self.st.get("total_n", 0), "baseline_mean": baseline,
              "text": f"{PNAME[key]} {fmt_param(key, old)} → {fmt_param(key, new)}"}
        self.st["changes"].append(ch)
        self.st["changes"] = self.st["changes"][-300:]
        self.st["pending"] = dict(ch)
        changes.append(ch)
        log.info("BRAIN change %s: %s -> %s (%s)", key, old, new, reason)
        return True

    def _evaluate_pending(self, lines):
        """Keep or roll back the last change once enough trades have been closed after it."""
        p = self.st.get("pending")
        if not p:
            return None
        after = [r for r in self.st["records"] if r["ts"] > p["ts"]]
        need = self.bc["eval_trades"]
        if len(after) < need:
            lines.append(f"• Sto verificando la modifica «{p['text']}»: {len(after)}/{need} trade dopo il cambio.")
            return None
        m_after = mean(r["pnl_pct"] for r in after[:need])
        base = p.get("baseline_mean")
        margin = self.bc["rollback_margin_pct"]
        status = "mantenuta"
        if base is not None and m_after < base - margin:
            status = "annullata"
            key = p["param"]
            if key not in self.locked:
                self.c[key] = p["old"]
                self.st["params"][key] = p["old"]
            lines.append(f"↩️ Annullo «{p['text']}»: dopo il cambio media {m_after:+.1f}% a trade contro "
                         f"{base:+.1f}% prima (peggio di oltre {margin:g} punti). Torno a {fmt_param(key, p['old'])}.")
        else:
            lines.append(f"✅ Tengo «{p['text']}»: dopo il cambio media {m_after:+.1f}% a trade"
                         + (f" contro {base:+.1f}% prima." if base is not None else "."))
        for c in reversed(self.st["changes"]):
            if c["ts"] == p["ts"] and c["param"] == p["param"]:
                c["status"], c["eval_mean"], c["eval_ts"] = status, round(m_after, 2), time.time()
                break
        self.st["pending"] = None
        return {"param": p["param"], "status": status, "after_mean": m_after, "baseline": base}

    # ------------------------------------------------------------------ analysis
    def analyze(self):
        total_n = self.st.get("total_n", len(self.st["records"]))
        win_size = min(int(self.bc.get("window", 50)), 50) if total_n > 100 else int(self.bc.get("window", 50))
        rows = self.st["records"][-win_size:]
        n = len(rows)
        interval = self._analysis_interval(total_n)
        cadence_due = (total_n - self.st.get("last_n", 0) >= interval)
        if cadence_due:
            self.st["last_n"] = total_n
        if n == 0:
            diag = "Nessun trade chiuso disponibile per l'analisi."
            self.st["diagnosis"] = diag
            self.save()
            return {"ts": time.time(), "n": 0, "win_rate": 0.0, "mean_pnl": 0.0, "text": diag,
                    "changes": [], "top_patterns": [], "categories": [], "phase": self.phase(),
                    "evaluation": None, "train_n": 0, "test_n": 0}
        mg = self.bc["min_group"]
        wins = sum(r["win"] for r in rows)
        avg = mean(r["pnl_pct"] for r in rows)
        lines = [f"Ho analizzato gli ultimi {n} trade: win rate {100 * wins / max(n, 1):.0f}%, risultato medio {avg:+.1f}% a trade.".replace(".", ",").replace("trade,", "trade.")]
        W = [r for r in rows if r["win"]]; L = [r for r in rows if not r["win"]]
        if W and L:
            def av(rs, k):
                v = [r[k] for r in rs if k in r]
                return sum(v) / len(v) if v else None
            for k in ("buy_slip_pct", "vol_ratio", "liquidity", "age_min"):
                a, b = av(L, k), av(W, k)
                if a is not None and b is not None:
                    lab, u = NUM[k]
                    lines.append(f"• Perdenti vs vincenti: {lab} in media {fmt(a, u)} contro {fmt(b, u)}.")
        evaluation = self._evaluate_pending(lines)
        phase = self.phase()
        adapt = cadence_due and phase == "apprendimento" and n >= self.bc["min_samples"]
        n_test = max(1, int(round(n * self.bc["oos_frac"])))
        train, test = rows[:-n_test], rows[-n_test:]
        stumps = self._stumps(train if adapt else rows, mg)
        cats = self._cats(rows, mg)
        changes = []
        min_gap = self.bc["min_gap_pct"]
        base_mean = mean(r["pnl_pct"] for r in rows[-self.bc["eval_trades"]:])
        # ---- entry filters: act on the strongest pattern that maps to a parameter AND is validated
        for s in stumps[:4]:
            lab, u = NUM[s["feat"]]
            side = "supera" if s["bad"] == "above" else "è sotto"
            txt = (f"Perdo soprattutto quando {lab} {side} {fmt(s['thr'], u)} "
                   f"(media {s['bad_mean']:+.0f}% su {s['n_bad']} trade, win rate {100 * s['bad_wr']:.0f}%, contro "
                   f"{s['good_mean']:+.0f}% e {100 * s['good_wr']:.0f}% negli altri {s['n_good']}).")
            if s["gap"] < min_gap or s["bad_mean"] >= 0:
                continue
            acted = ""
            rule = ENTRY_RULES.get((s["feat"], s["bad"]))
            if (s["feat"], s["bad"]) == ("buy_slip_pct", "above") and slippage_rule_on(self.bc):
                rule = ("max_entry_slippage_pct", "to_threshold")
            if adapt and rule and not changes:
                if s["t"] < self.bc["min_t"]:
                    acted = f" (differenza non ancora significativa: t={s['t']:.1f} < {self.bc['min_t']:g}, non cambio nulla)"
                else:
                    ok, why = self._oos_confirms(s, test)
                    if not ok:
                        acted = f" ({why}: non cambio nulla)"
                    else:
                        key, how = rule
                        if how == "raise_liq":
                            liq = sorted(r["liquidity"] for r in train if r.get("buy_slip_pct", 0) <= s["thr"] and r["liquidity"] > 0)
                            target = liq[len(liq) // 2] if liq else self.c.get(key, 0) + 5000
                            target = max(target, self.c.get(key, 0) + 2000)
                        else:
                            target = s["thr"]
                        if self._change(key, target, txt + " " + why, changes, baseline=base_mean):
                            acted = f" {why[0].upper() + why[1:]}. → Cambio {changes[-1]['text']} (lo verifico sui prossimi {self.bc['eval_trades']} trade)."
            lines.append(txt + acted)
            if changes:
                break
        for cst in cats[:2]:
            if cst["gap"] >= min_gap and cst["worst_mean"] < 0:
                lines.append(f"• Con {CAT[cst['feat']]} «{cst['worst']}» vado peggio ({cst['worst_mean']:+.0f}% medio su "
                             f"{cst['n']} trade) che con «{cst['best']}» ({cst['best_mean']:+.0f}%). Lo tengo d'occhio.")
        # ---- exits (only volume_spike trades; copy trades have their own exits).
        # Partition directly from disjoint `train` and `test` slices to avoid dict value-equality collisions.
        vtrain = [r for r in train if r.get("entry_type") != "copy_top_traders"]
        vtest = [r for r in test if r.get("entry_type") != "copy_top_traders"]
        vrows = vtrain + vtest

        def stats(rs):
            ex = {}
            for r in rs:
                ex.setdefault(r["exit_reason"], []).append(r)
            nx = max(len(rs), 1)
            return ex, nx

        if adapt and not changes and len(vrows) >= self.bc["min_samples"] // 2:
            ex, nx = stats(vrows); ex1, nx1 = stats(vtrain); ex2, nx2 = stats(vtest)
            share = lambda e, x, k: len(e.get(k, [])) / x
            mo = lambda e, k: mean(r["pnl_pct"] for r in e.get(k, []))
            if share(ex, nx, "take_profit") >= 0.3 and share(ex2, nx2, "take_profit") >= 0.2:
                self._change("take_profit_pct", self.c["take_profit_pct"] + 0.05,
                             f"Il take profit scatta spesso ({100 * share(ex, nx, 'take_profit'):.0f}% dei trade, anche fuori campione): alzo l'obiettivo.", changes, baseline=base_mean)
            elif share(ex, nx, "take_profit") == 0 and share(ex, nx, "trailing_stop") == 0 and nx >= 20:
                self._change("take_profit_pct", self.c["take_profit_pct"] - 0.05,
                             f"Nessuno dei {nx} trade arriva al take profit: abbasso un po' l'obiettivo.", changes, baseline=base_mean)
            sl = ex.get("stop_loss", [])
            if len(sl) >= mg:
                gap = mean(r["price_move_pct"] or 0 for r in sl)
                if gap < -100 * self.c["stop_loss_pct"] * 2.5:
                    lines.append(f"• Gli stop loss escono in media a {gap:.0f}% di prezzo, molto peggio dello stop: "
                                 "crolli troppo veloci per il controllo ogni ~2 s (limite dei dati pubblici).")
            for c in changes:
                lines.append(f"{c['reason']} → Cambio {c['text']} (lo verifico sui prossimi {self.bc['eval_trades']} trade).")
        # ---- loosen when things go well (more trades), one step toward the original config
        if adapt and not changes and n and wins / max(n, 1) >= 0.55 and avg > 2 and mean(r["pnl_pct"] for r in test) > 0:
            for key in ("spike_multiplier", "min_liquidity_usd", "max_top10_pct"):
                if key in self._orig and self.c.get(key) != self._orig[key]:
                    if self._change(key, self._orig[key], "Le cose vanno bene (anche negli ultimi trade): allento di un passo un filtro stretto prima.", changes, baseline=base_mean):
                        lines.append(f"Va bene così: allento di un passo {changes[-1]['text']}.")
                    break
        if not changes:
            if phase == "osservazione":
                lines.append(f"Modalità osservazione: analizzo ma non cambio nulla finché non ho {self.bc['observe_trades']} trade "
                             f"(ora {self.st.get('total_n', 0)}).")
            elif phase == "verifica modifica":
                lines.append("Nessun nuovo cambiamento finché la modifica precedente non è verificata.")
            elif phase == "solo analisi":
                lines.append("Modalità solo analisi (adapt: false): nessun parametro viene cambiato.")
            else:
                lines.append("Nessun cambiamento: non c'è uno schema abbastanza chiaro e confermato (servono più trade).")
        diag = "\n".join(lines)
        a = {"ts": time.time(), "n": n, "win_rate": 100 * wins / max(n, 1), "mean_pnl": avg, "text": diag,
             "changes": changes, "top_patterns": stumps[:5], "categories": cats[:3], "phase": phase,
             "evaluation": evaluation, "train_n": len(train), "test_n": len(test)}
        self.st["analyses"].append(a)
        self.st["analyses"] = self.st["analyses"][-50:]
        self.st["diagnosis"] = diag
        self.save()
        log.info("BRAIN analysis on %d trades (%s), %d change(s)%s", n, phase, len(changes),
                 f", evaluation: {evaluation['status']}" if evaluation else "")
        return a

    def summary(self):
        n = self.st.get("total_n", len(self.st["records"]))
        interval = self._analysis_interval(n)
        return {"enabled": self.enabled, "records": n,
                "analysis_interval": interval,
                "next_in": max(0, max(interval - (n - self.st["last_n"]),
                                      self.bc["min_samples"] - n)),
                "diagnosis": self.st["diagnosis"], "phase": self.phase(), "pending": self.st.get("pending"),
                "last_ts": self.st["analyses"][-1]["ts"] if self.st["analyses"] else None,
                "changes": self.st["changes"][-30:], "params": self.st["params"], "locked": sorted(self.locked),
                "mode": "validated"}


class LegacyBrain:
    """v4 cervello (default, brain.mode: legacy) with dynamic frequency scaling, zero-division protection,
    and enriched execution/slippage/quote-source telemetry."""
    mode = "legacy"

    def __init__(self, cfg, data_dir, tz=None, locked=None):
        self.c = cfg
        self.bc = cfg.get("brain") or {}
        self.path = os.path.join(data_dir, "brain.json")
        self.tz = tz
        self.st = {"records": [], "analyses": [], "changes": [], "params": {}, "last_n": 0, "total_n": 0, "diagnosis": ""}
        obj, src = load_json_safe(self.path)
        if obj:
            self.st.update(obj)
            if not self.st.get("total_n"):
                self.st["total_n"] = len(self.st["records"])
        elif src == "corrupt":
            log.warning("brain.json unreadable: starting a fresh brain")
        self.bc["_orig"] = {k: cfg[k] for k in BOUNDS if cfg.get(k) is not None}   # for loosening back
        for k, v in self.st["params"].items():      # learned values survive restarts
            if k in BOUNDS:
                cfg[k] = v

    @property
    def enabled(self):
        return self.bc.get("enabled", True)

    def save(self):
        atomic_json(self.path, self.st)

    def lock_param(self, key):          # legacy behaviour: /imposta does not lock anything
        pass

    def phase(self):
        return "legacy" if self.enabled else "spento"

    # ------------------------------------------------------------------ data
    def record(self, pos, trade):
        if not self.enabled or trade["exit_reason"] in ("end_of_run", "manual_close"):
            return
        r = build_trade_record(pos, trade, self.tz)
        self.st["records"].append(r)
        self.st["records"] = self.st["records"][-1000:]
        self.st["total_n"] = self.st.get("total_n", 0) + 1
        self._update_perf_after()
        self.save()

    def _analysis_interval(self, n):
        return compute_dynamic_interval(n, self.st.get("last_n", 0), self.bc)

    def due(self):
        n = self.st.get("total_n", len(self.st["records"]))
        interval = self._analysis_interval(n)
        return self.enabled and n >= self.bc.get("min_trades", 10) and n - self.st["last_n"] >= interval

    # ------------------------------------------------------------------ analysis
    def _stumps(self, rows, mg):
        out = []
        for feat in NUM:
            vals = sorted({r[feat] for r in rows if feat in r})
            if len(vals) < 4:
                continue
            for q in (0.25, 0.4, 0.5, 0.6, 0.75):
                thr = vals[int(q * (len(vals) - 1))]
                lo = [r for r in rows if feat in r and r[feat] <= thr]
                hi = [r for r in rows if feat in r and r[feat] > thr]
                if len(lo) < mg or len(hi) < mg:
                    continue
                mlo = sum(r["pnl_pct"] for r in lo) / max(len(lo), 1)
                mhi = sum(r["pnl_pct"] for r in hi) / max(len(hi), 1)
                wlo = sum(r["win"] for r in lo) / max(len(lo), 1)
                whi = sum(r["win"] for r in hi) / max(len(hi), 1)
                bad = "above" if mhi < mlo else "below"
                out.append({"feat": feat, "thr": thr, "bad": bad, "gap": abs(mhi - mlo),
                            "bad_mean": min(mhi, mlo), "good_mean": max(mhi, mlo),
                            "bad_wr": whi if bad == "above" else wlo, "good_wr": wlo if bad == "above" else whi,
                            "n_bad": len(hi) if bad == "above" else len(lo), "n_good": len(lo) if bad == "above" else len(hi)})
        best = {}
        for s in out:   # best threshold per feature
            if s["feat"] not in best or s["gap"] > best[s["feat"]]["gap"]:
                best[s["feat"]] = s
        return sorted(best.values(), key=lambda s: -s["gap"])

    def _cats(self, rows, mg):
        out = []
        for feat in CAT:
            groups = {}
            for r in rows:
                groups.setdefault(r.get(feat, "?"), []).append(r)
            g = {k: v for k, v in groups.items() if len(v) >= mg}
            if len(g) < 2:
                continue
            means = {k: sum(r["pnl_pct"] for r in v) / max(len(v), 1) for k, v in g.items()}
            worst, bestk = min(means, key=means.get), max(means, key=means.get)
            out.append({"feat": feat, "worst": worst, "best": bestk, "worst_mean": means[worst],
                        "best_mean": means[bestk], "gap": means[bestk] - means[worst], "n": len(g[worst])})
        return sorted(out, key=lambda s: -s["gap"])

    def _change(self, key, new, reason, changes):
        lo, hi, step = BOUNDS[key]
        old = self.c.get(key)
        if key in NULLABLE:
            new = _slip_target(old, new)
        elif old is None:
            return False
        else:
            new = max(lo, min(hi, new))
            new = max(old - step, min(old + step, new))      # never jump more than one step
            new = round(new, 4) if isinstance(old, float) or key.endswith("_pct") else round(new)
        if old is not None and abs(new - old) < 1e-9:
            return False
        # Do not re-apply changes that previously failed and were rolled back (unless restoring original config)
        orig_map = getattr(self, "_orig", None) or self.bc.get("_orig", {})
        is_restoring_orig = (key in orig_map and orig_map[key] == new)
        if not is_restoring_orig and any(c.get("param") == key and c.get("new") == new and c.get("status") == "annullata"
               for c in self.st.get("changes", [])):
            log.info("BRAIN: %s -> %s was previously rolled back (failed experiment), skipping", key, new)
            return False
        self.c[key] = new
        self.st["params"][key] = new
        ch = {"ts": time.time(), "param": key, "old": old, "new": new, "reason": reason,
              "text": f"{PNAME[key]} {fmt_param(key, old)} → {fmt_param(key, new)}"}
        self.st.setdefault("change_log", []).append({**ch, **self._change_context()})
        self.st["change_log"] = self.st["change_log"][-300:]
        self.st["changes"].append(ch)
        self.st["changes"] = self.st["changes"][-300:]
        changes.append(ch)
        log.info("BRAIN change %s: %s -> %s (%s)", key, old, new, reason)
        return True

    def _change_context(self):
        rows = self.st["records"][-self.bc.get("window", 50):]
        n = len(rows)
        ev = getattr(self, "_evidence", None)
        return {"time": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime()), "n_trades_used": n,
                "perf_before": {"n": n, "mean_pnl_pct": round(sum(r["pnl_pct"] for r in rows) / max(n, 1), 2) if n else None,
                                "win_rate_pct": round(100 * sum(r["win"] for r in rows) / max(n, 1), 1) if n else None},
                "evidence": ({k: (round(v, 3) if isinstance(v, float) else v) for k, v in ev.items()}
                             if isinstance(ev, dict) else ev),
                "perf_after": None}

    def _update_perf_after(self, need=10):
        """Performance AFTER each change: mean P&L / win rate of the first `need` trades closed after it."""
        for c in self.st.get("change_log", [])[-50:]:
            if c.get("perf_after") is None and "perf_before" in c:
                after = [r for r in self.st["records"] if r["ts"] > c["ts"]][:need]
                if len(after) >= need:
                    c["perf_after"] = {"n": need, "mean_pnl_pct": round(sum(r["pnl_pct"] for r in after) / need, 2),
                                       "win_rate_pct": round(100 * sum(r["win"] for r in after) / need, 1)}

    def analyze(self):
        total_n = self.st.get("total_n", len(self.st["records"]))
        win_size = min(int(self.bc.get("window", 50)), 50) if total_n > 100 else int(self.bc.get("window", 50))
        rows = self.st["records"][-win_size:]
        n = len(rows)
        self.st["last_n"] = total_n
        if n == 0:
            diag = "Nessun trade chiuso disponibile per l'analisi."
            self.st["diagnosis"] = diag
            self.save()
            return {"ts": time.time(), "n": 0, "win_rate": 0.0, "mean_pnl": 0.0, "text": diag,
                    "changes": [], "top_patterns": [], "categories": []}
        mg = self.bc.get("min_group", 3)
        adapt = self.bc.get("adapt", True)
        wins = sum(r["win"] for r in rows)
        safe_n = max(n, 1)
        mean_pnl = sum(r["pnl_pct"] for r in rows) / safe_n
        lines = [f"Ho analizzato gli ultimi {n} trade: win rate {100 * wins / safe_n:.0f}%, risultato medio {mean_pnl:+.1f}% a trade.".replace(".", ",").replace("trade,", "trade.")]
        W = [r for r in rows if r["win"]]; L = [r for r in rows if not r["win"]]
        if W and L:
            def avg(rs, k):
                v = [r[k] for r in rs if k in r]
                return sum(v) / len(v) if v else None
            for k in ("buy_slip_pct", "vol_ratio", "liquidity", "age_min"):
                a, b = avg(L, k), avg(W, k)
                if a is not None and b is not None:
                    lab, u = NUM[k]
                    lines.append(f"• Perdenti vs vincenti: {lab} in media {fmt(a, u)} contro {fmt(b, u)}.")
        stumps = self._stumps(rows, mg)
        cats = self._cats(rows, mg)
        changes = []
        min_gap = self.bc.get("min_gap_pct", 8)
        # ---- entry filters: act on the strongest pattern that maps to a parameter
        for s in stumps[:4]:
            lab, u = NUM[s["feat"]]
            side = "supera" if s["bad"] == "above" else "è sotto"
            txt = (f"Perdo soprattutto quando {lab} {side} {fmt(s['thr'], u)} "
                   f"(media {s['bad_mean']:+.0f}% su {s['n_bad']} trade, win rate {100 * s['bad_wr']:.0f}%, contro "
                   f"{s['good_mean']:+.0f}% e {100 * s['good_wr']:.0f}% negli altri {s['n_good']}).")
            if s["gap"] < min_gap or s["bad_mean"] >= 0:
                continue
            rule = ENTRY_RULES.get((s["feat"], s["bad"]))
            if (s["feat"], s["bad"]) == ("buy_slip_pct", "above") and slippage_rule_on(self.bc):
                rule = ("max_entry_slippage_pct", "to_threshold")      # the direct lever: cap the entry slippage
            acted = ""
            if adapt and rule and not any(c["param"] in BOUNDS and c["param"] not in ("max_hold_minutes", "take_profit_pct", "stop_loss_pct") for c in changes):
                key, how = rule
                if how == "raise_liq":
                    liq = sorted(r["liquidity"] for r in rows if r.get("buy_slip_pct", 0) <= s["thr"] and r["liquidity"] > 0)
                    target = liq[len(liq) // 2] if liq else self.c.get(key, 0) + 5000
                    target = max(target, self.c.get(key, 0) + 2000)
                else:
                    target = s["thr"]
                self._evidence = s
                if self._change(key, target, txt, changes):
                    acted = f" → Cambio {changes[-1]['text']}."
            lines.append(txt + acted)
            if acted:
                break
        for cst in cats[:2]:
            if cst["gap"] >= min_gap and cst["worst_mean"] < 0:
                lines.append(f"• Con {CAT[cst['feat']]} «{cst['worst']}» vado peggio ({cst['worst_mean']:+.0f}% medio su "
                             f"{cst['n']} trade) che con «{cst['best']}» ({cst['best_mean']:+.0f}%). Lo tengo d'occhio.")
        # ---- exits
        ex = {}
        vrows = [r for r in rows if r.get("entry_type") != "copy_top_traders"]
        for r in vrows:
            ex.setdefault(r["exit_reason"], []).append(r)
        nx = max(len(vrows), 1)
        share = lambda k: len(ex.get(k, [])) / nx
        self._evidence = {"exit_shares_pct": {k: round(100 * len(v) / nx, 1) for k, v in ex.items()},
                          "exit_mean_pnl_pct": {k: round(sum(r["pnl_pct"] for r in v) / max(len(v), 1), 2) for k, v in ex.items()},
                          "n_volume_spike_trades": len(vrows)}
        mean_of = lambda k: sum(r["pnl_pct"] for r in ex[k]) / max(len(ex[k]), 1) if ex.get(k) else 0
        if adapt:
            if share("take_profit") >= 0.3:
                self._change("take_profit_pct", self.c["take_profit_pct"] + 0.05,
                             f"Il take profit scatta spesso ({100 * share('take_profit'):.0f}% dei trade): alzo l'obiettivo.", changes)
            elif share("take_profit") == 0 and share("trailing_stop") == 0 and nx >= 10:
                self._change("take_profit_pct", self.c["take_profit_pct"] - 0.05,
                             "Nessun trade arriva al take profit: abbasso un po' l'obiettivo.", changes)
            sl = ex.get("stop_loss", [])
            if len(sl) >= mg:
                gap = sum(r["price_move_pct"] or 0 for r in sl) / max(len(sl), 1)
                if gap < -100 * self.c["stop_loss_pct"] * 2.5:
                    lines.append(f"• Gli stop loss escono in media a {gap:.0f}% di prezzo, molto peggio dello stop: "
                                 "crolli troppo veloci per il controllo ogni ~2 s (limite dei dati pubblici).")
        exit_changes = [c for c in changes if c["param"] in ("max_hold_minutes", "take_profit_pct", "stop_loss_pct")]
        for c in exit_changes:
            lines.append(f"{c['reason']} → Cambio {c['text']}.")
        # ---- loosen when things go well (more trades), one step toward the original config
        if adapt and not changes and wins / safe_n >= 0.55 and mean_pnl > 2:
            orig = self.bc.get("_orig", {})
            for key in ("spike_multiplier", "min_liquidity_usd", "max_top10_pct"):
                if key in orig and self.c.get(key) != orig[key]:
                    if self._change(key, orig[key], "Le cose vanno bene: allento di un passo un filtro stretto prima.", changes):
                        lines.append(f"Va bene così: allento di un passo {changes[-1]['text']}.")
                    break
        if not changes:
            lines.append("Nessun cambiamento: non c'è ancora uno schema abbastanza chiaro (servono più trade).")
        diag = "\n".join(lines)
        a = {"ts": time.time(), "n": n, "win_rate": 100 * wins / safe_n, "mean_pnl": mean_pnl, "text": diag,
             "changes": changes, "top_patterns": stumps[:5], "categories": cats[:3]}
        self.st["analyses"].append(a)
        self.st["analyses"] = self.st["analyses"][-50:]
        self.st["diagnosis"] = diag
        self.save()
        log.info("BRAIN analysis on %d trades, %d change(s)", n, len(changes))
        return a

    def startup_slippage_check(self):
        """Once, at start-up: if the slippage limit was never set, run the brain's slippage analysis on the
        current window now (instead of waiting for the next scheduled analysis). Returns the change or None."""
        if not (self.enabled and self.bc.get("adapt", True) and slippage_rule_on(self.bc)):
            return None
        if self.c.get("max_entry_slippage_pct") is not None or self.st.get("slip_init_done"):
            return None
        rows = self.st["records"][-self.bc.get("window", 50):]
        if len(rows) < self.bc.get("min_trades", 10):
            return None
        changes = []
        s = entry_slippage_pass(self, rows, self.bc.get("min_group", 3), self.bc.get("min_gap_pct", 8), changes)
        self.st["slip_init_done"] = time.time()
        if changes:
            changes[0]["data"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in s.items()}
            self.st["diagnosis"] = (self.st.get("diagnosis") or "") + "\n" + changes[0]["reason"] +                 f" → Cambio {changes[0]['text']}."
        self.save()
        return changes[0] if changes else None

    def summary(self):
        n = self.st.get("total_n", len(self.st["records"]))
        interval = self._analysis_interval(n)
        return {"enabled": self.enabled, "records": n,
                "analysis_interval": interval,
                "next_in": max(0, interval - (n - self.st["last_n"])),
                "diagnosis": self.st["diagnosis"],
                "last_ts": self.st["analyses"][-1]["ts"] if self.st["analyses"] else None,
                "changes": self.st["changes"][-30:], "params": self.st["params"], "phase": self.phase(), "mode": "legacy"}


def slippage_rule_on(bc):
    env = os.environ.get("BRAIN_ENTRY_SLIPPAGE_LIMIT", "").strip().lower()
    if env:                                       # .env wins (VPS default: 1)
        return env in ("1", "true", "yes", "on")
    return bool((bc or {}).get("entry_slippage_limit", False))


def _slip_target(old, thr):
    lo, hi, step = BOUNDS["max_entry_slippage_pct"]
    new = max(lo, min(hi, float(thr)))
    if old is not None:
        new = max(old - step, min(old + step, new))      # once set: never more than one step per change
    return round(new, 2)


def entry_slippage_pass(brain, rows, mg, min_gap, changes, **kw):
    """The brain's own slippage pattern -> max_entry_slippage_pct. Used inside analyze() and once at start-up
    (when the limit was never set). Returns the stump used (or None)."""
    st_ = [s for s in brain._stumps(rows, mg) if s["feat"] == "buy_slip_pct"]
    if not st_:
        return None
    s = st_[0]
    if s["bad"] != "above" or s["gap"] < min_gap or s["bad_mean"] >= 0:
        return None
    txt = (f"Perdo soprattutto quando lo slippage d'acquisto supera {fmt(s['thr'], '%')} "
           f"(media {s['bad_mean']:+.0f}% su {s['n_bad']} trade, win rate {100 * s['bad_wr']:.0f}%, contro "
           f"{s['good_mean']:+.0f}% e {100 * s['good_wr']:.0f}% negli altri {s['n_good']}): non entro più sopra questo "
           "slippage.")
    brain._evidence = s
    brain._change("max_entry_slippage_pct", s["thr"], txt, changes, **kw)
    return s


def Brain(cfg, data_dir, tz=None, locked=None):
    """brain.mode: validated (DEFAULT = observation, out-of-sample validation, rollback) | legacy (original v3 behaviour)."""
    mode = str((cfg.get("brain") or {}).get("mode", "validated")).lower()
    return (ValidatedBrain if mode == "validated" else LegacyBrain)(cfg, data_dir, tz, locked)
