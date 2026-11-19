"""Risk Manager for the PAPER bot - independent from the strategy and from the 'cervello'.

The strategy proposes an entry; the Risk Manager decides IF it may happen and HOW BIG it may be.
Limits are read ONCE from config (risk.<mode>) and frozen in this object: the brain only edits strategy
parameters in the shared config dict and can never reach them; /imposta size is clamped to max_position_pct.

Two modes (config: risk.mode):
- survival (DEFAULT): no position limit, no daily stop, no drawdown stop, no pause after losses;
  the bot only dies when the money is gone. Duplicate-buy protection (same token already open or
  being bought) stays on in both modes as a correctness guard.
- conservative (opt-in, OFF unless risk.mode: conservative): max open positions, max total exposure,
  max size per trade, max daily loss, max drawdown, max consecutive losses, duplicate-buy window,
  automatic pause + controlled resume.
Paper trading only: nothing here can touch real money.
"""
import logging
import math
import time
from datetime import datetime, timedelta

log = logging.getLogger("paperbot.risk")

CONSERVATIVE = {
    "max_open_positions": 10,         # concurrent positions
    "max_exposure_pct": 0.75,         # sum of open cost basis <= 75% of equity
    "max_position_pct": 0.15,         # one trade <= 15% of equity (also caps /imposta size)
    "max_daily_loss_pct": 0.15,       # equity -15% vs start of the (local) day -> pause until tomorrow
    "max_drawdown_pct": 0.35,         # equity -35% from peak -> pause, manual /riprendi forza needed
    "max_consecutive_losses": 5,      # 5 losing trades in a row -> pause...
    "loss_pause_minutes": 45,         # ...for 45 minutes, then automatic controlled resume
    "duplicate_window_s": 120,        # same token cannot be bought twice within 120 s
    "resume_size_factor": 0.5,        # after any risk pause: half size...
    "resume_trades": 3,               # ...for the next 3 trades
    "drawdown_alert_steps_pct": [10, 20, 30, 50, 70],
}
SURVIVAL = {
    "max_open_positions": None, "max_exposure_pct": None, "max_position_pct": None,
    "max_daily_loss_pct": None, "max_drawdown_pct": None, "max_consecutive_losses": None,
    "loss_pause_minutes": 0, "duplicate_window_s": 0, "resume_size_factor": 1.0, "resume_trades": 0,
    "drawdown_alert_steps_pct": [20, 40, 60, 80],
}
# hard ceilings: even a hand-edited config cannot go beyond these in conservative mode
CEIL = {"max_position_pct": 0.5, "max_exposure_pct": 1.0, "max_open_positions": 50,
        "max_daily_loss_pct": 0.9, "max_drawdown_pct": 0.95, "max_consecutive_losses": 50}

PAUSE_LABEL = {"daily_loss": "perdita giornaliera massima", "drawdown": "drawdown massimo",
               "consecutive_losses": "troppe perdite consecutive", "disk": "disco quasi pieno",
               "manual": "pausa manuale"}


def _is_valid_number(val, allow_negative=True):
    """Returns True only for finite int/float values (strictly rejecting bool, NaN, inf)."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return False
    if not math.isfinite(val):
        return False
    if not allow_negative and val < 0:
        return False
    return True


class RiskManager:
    def __init__(self, cfg, state=None, tz=None, clock=time.time):
        rc = cfg.get("risk") or {}
        self.mode = str(rc.get("mode", "survival")).lower()
        if self.mode not in ("conservative", "survival"):
            log.warning("risk.mode '%s' sconosciuto: uso survival (comportamento attuale)", self.mode)
            self.mode = "survival"
        base = dict(CONSERVATIVE if self.mode == "conservative" else SURVIVAL)
        base.update({k: v for k, v in (rc.get(self.mode) or {}).items() if k in base})
        if self.mode == "conservative":
            for k, ceil in CEIL.items():
                if base.get(k) is None or base[k] > ceil:
                    base[k] = ceil if base.get(k) is not None else CONSERVATIVE[k]
        self.L = dict(base)                    # frozen limits (copy; never shared with cfg)
        self.tz, self.clock = tz, clock
        self.s = {"day": "", "day_start_equity": None, "peak_equity": None, "consec_losses": 0,
                  "pause": None, "resume_left": 0, "recent_buys": {}, "dd_alerted": 0,
                  "triggers": []}
        if isinstance(state, dict):
            self.s.update({k: v for k, v in state.items() if k in self.s})
        self.inflight = set()
        self.disk_level = "ok"
        self.pause_on_disk = bool(rc.get("pause_on_disk_critical", True))
        self.outbox = []                       # events waiting to be notified (drained by the bot)

    def pop_events(self):
        ev, self.outbox = self.outbox, []
        return ev

    # ---------------------------------------------------------------- helpers
    def limits(self):
        return dict(self.L)

    def _today(self):
        return datetime.fromtimestamp(self.clock(), self.tz).strftime("%Y-%m-%d")

    def _next_midnight(self):
        d = datetime.fromtimestamp(self.clock(), self.tz)
        nxt = (d.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()
        return nxt

    def state(self):
        s = dict(self.s)
        s["recent_buys"] = {m: t for m, t in self.s["recent_buys"].items() if self.clock() - t < 3600}
        return s

    @property
    def paused(self):
        return self.s["pause"] is not None

    def pause_info(self):
        return self.s["pause"]

    # ---------------------------------------------------------------- sizing
    def max_position_pct(self, requested):
        lim = self.L["max_position_pct"]
        return min(requested, lim) if lim else requested

    def size(self, equity, cash, open_cost, requested_pct, min_trade):
        """Returns (size_usd, note). size 0 = do not trade."""
        pct = self.max_position_pct(requested_pct)
        size = equity * pct
        note = []
        if pct < requested_pct:
            note.append(f"size limitata al {pct:.0%}")
        if self.s["resume_left"] > 0 and self.L["resume_size_factor"] < 1:
            size *= self.L["resume_size_factor"]
            note.append("ripresa controllata")

        # Maximum position safety ceiling: min_trade must NEVER override this limit
        max_allowed = equity * self.L["max_position_pct"] if self.L.get("max_position_pct") is not None else float("inf")
        if self.s["resume_left"] > 0 and self.L["resume_size_factor"] < 1 and self.L.get("max_position_pct") is not None:
            max_allowed *= self.L["resume_size_factor"]

        if min_trade > max_allowed:
            return 0.0, ", ".join(note + [f"minimo ({min_trade}) supera limite massimo ({max_allowed:.2f})"])

        size = max(size, min_trade)
        size = min(size, max_allowed)

        if self.L["max_exposure_pct"]:
            room = equity * self.L["max_exposure_pct"] - open_cost
            if room < size:
                size = room
                note.append("limite esposizione")
        size = min(size, cash)
        if size < min_trade:
            return 0.0, ", ".join(note + ["sotto il minimo"])
        return round(size, 4), ", ".join(note)

    # ---------------------------------------------------------------- entry gate
    def check_entry(self, mint, positions, equity, cash, open_cost):
        """(ok, reason). Called before every simulated buy."""
        self.tick(equity)
        if self.paused:
            return False, "risk_paused:" + self.s["pause"]["reason"]
        if mint in positions or mint in self.inflight:
            return False, "duplicate_open"
        w = self.L["duplicate_window_s"]
        last = self.s["recent_buys"].get(mint)
        if w and last and self.clock() - last < w:
            return False, "duplicate_recent_buy"
        mx = self.L["max_open_positions"]
        if mx and len(positions) >= mx:
            return False, "max_open_positions"
        if self.L["max_exposure_pct"] and open_cost >= equity * self.L["max_exposure_pct"]:
            return False, "max_exposure"
        return True, ""

    def begin_buy(self, mint):
        self.inflight.add(mint)

    def end_buy(self, mint, filled):
        self.inflight.discard(mint)
        if filled:
            self.s["recent_buys"][mint] = self.clock()
            if len(self.s["recent_buys"]) > 500:
                cut = self.clock() - 3600
                self.s["recent_buys"] = {m: t for m, t in self.s["recent_buys"].items() if t >= cut}
            if self.s["resume_left"] > 0:
                self.s["resume_left"] -= 1

    # ---------------------------------------------------------------- events
    def _pause(self, reason, text, until=None, manual=False):
        if self.paused and self.s["pause"]["reason"] == reason:
            return None
        self.s["pause"] = {"reason": reason, "since": self.clock(), "until": until, "manual_resume": manual,
                           "text": text}
        ev = {"kind": "pause", "reason": reason, "text": text, "until": until, "manual": manual,
              "ts": self.clock(), "mode": self.mode}
        self.s["triggers"] = (self.s["triggers"] + [ev])[-50:]
        log.warning("RISK PAUSE (%s): %s", reason, text)
        return ev

    def tick(self, equity, disk_level=None):
        """Periodic check (equity marked to market). Events go to self.outbox (and are returned)."""
        if disk_level is not None:
            self.disk_level = disk_level
        if not _is_valid_number(equity, allow_negative=False):
            log.warning("tick ignorato: equity non valida (%r)", equity)
            return []
        disk_level = self.disk_level
        evs = []
        now = self.clock()
        today = self._today()
        if self.s["day"] != today:
            self.s["day"], self.s["day_start_equity"] = today, equity
        if self.s["peak_equity"] is None or equity > self.s["peak_equity"]:
            self.s["peak_equity"], self.s["dd_alerted"] = equity, 0
        # automatic resumes
        p = self.s["pause"]
        if p and p.get("until") and now >= p["until"]:
            evs.append(self._resume_ev(p["reason"], auto=True))
        if p and p["reason"] == "disk" and disk_level != "critical":
            evs.append(self._resume_ev("disk", auto=True))
        # drawdown alerts (both modes)
        peak = self.s["peak_equity"] or equity
        dd = 100 * (1 - equity / peak) if peak > 0 else 0
        steps = [s for s in self.L.get("drawdown_alert_steps_pct") or [] if dd >= s]
        if steps and max(steps) > self.s["dd_alerted"]:
            self.s["dd_alerted"] = max(steps)
            evs.append({"kind": "drawdown", "dd_pct": dd, "step": max(steps), "peak": peak, "equity": equity,
                        "ts": now, "mode": self.mode})
        # limits
        if disk_level == "critical" and self.pause_on_disk:
            e = self._pause("disk", "spazio su disco critico: niente nuovi acquisti finché non si libera spazio")
            e and evs.append(e)
        ml = self.L["max_daily_loss_pct"]
        ds = self.s["day_start_equity"]
        if ml and ds and equity <= ds * (1 - ml):
            e = self._pause("daily_loss", f"capitale −{100 * (1 - equity / ds):.1f}% da inizio giornata "
                            f"(limite −{100 * ml:.0f}%)", until=self._next_midnight())
            e and evs.append(e)
        md = self.L["max_drawdown_pct"]
        if md and dd >= 100 * md:
            e = self._pause("drawdown", f"drawdown −{dd:.1f}% dal massimo (limite −{100 * md:.0f}%)", manual=True)
            e and evs.append(e)
        evs = [e for e in evs if e]
        self.outbox += evs
        return evs

    def on_trade_closed(self, pnl_usd, equity):
        if not _is_valid_number(pnl_usd, allow_negative=True) or not _is_valid_number(equity, allow_negative=False):
            log.warning("on_trade_closed ignorato: input non validi (pnl_usd=%r, equity=%r)", pnl_usd, equity)
            return []
        evs = []
        if pnl_usd > 0:
            self.s["consec_losses"] = 0
        else:
            self.s["consec_losses"] += 1
            mc = self.L["max_consecutive_losses"]
            if mc and self.s["consec_losses"] >= mc:
                mins = self.L["loss_pause_minutes"] or 60
                e = self._pause("consecutive_losses", f"{self.s['consec_losses']} trade in perdita di fila "
                                f"(limite {mc}): pausa di {mins} min", until=self.clock() + mins * 60)
                if e:
                    self.s["consec_losses"] = 0
                    evs.append(e)
                    self.outbox.append(e)
        evs += self.tick(equity)
        return evs

    def _resume_ev(self, reason, auto=False, by_user=False):
        self.s["pause"] = None
        self.s["resume_left"] = self.L["resume_trades"]
        if reason == "drawdown":
            self.s["peak_equity"] = None          # new reference, otherwise the limit re-triggers at once
        if reason == "daily_loss":
            self.s["day"] = ""                    # new day reference
        ev = {"kind": "resume", "reason": reason, "auto": auto, "by_user": by_user, "ts": self.clock(),
              "resume_trades": self.L["resume_trades"], "factor": self.L["resume_size_factor"], "mode": self.mode}
        self.s["triggers"] = (self.s["triggers"] + [ev])[-50:]
        log.warning("RISK RESUME (%s, %s)", reason, "auto" if auto else "manual")
        return ev

    def user_resume(self, force=False):
        """(ok, message_kind). Manual /riprendi: auto-pauses wait for their timer unless force."""
        p = self.s["pause"]
        if not p:
            return True, "not_paused"
        if (p.get("manual_resume") or p.get("until")) and not force:
            return False, "needs_force"
        self.outbox.append(self._resume_ev(p["reason"], by_user=True))
        return True, "resumed"

    def summary(self):
        p = self.s["pause"]
        return {"mode": self.mode, "limits": self.limits(), "paused": bool(p), "pause": p,
                "consec_losses": self.s["consec_losses"], "peak_equity": self.s["peak_equity"],
                "day_start_equity": self.s["day_start_equity"], "resume_left": self.s["resume_left"],
                "triggers": self.s["triggers"][-10:]}