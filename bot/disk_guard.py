"""Disk monitor (VPS): when disk usage passes a threshold (default 80%) -> Telegram alert, then compress OLD data.

Only already-rotated / archived files are touched; the live files the bot writes are never compressed or deleted:
  compressed (gzip, moved to data/archive/ with a timestamp so later rotations never clash):
    bot.log.N, telegram_outbox.log.N, *.jsonl.N, *.out.N (rotated logs), trades.<ts>.csv (old-version trade files)
  packed: backups/<dir> older than the newest `keep_backups` -> data/archive/backups_<first>_<last>.tar.gz
  NEVER touched: everything else (state.json, state.json.bak, brain.json, trades.csv, heartbeat.json, *.lock,
  telegram_pending.json, antirug_*.json/jsonl being written, risk*.json, fx.json, ...).
A file is removed only after its compressed copy was written and re-read successfully."""
import gzip, logging, os, re, shutil, tarfile, time

log = logging.getLogger("paperbot.disk")

DEFAULTS = {"enabled": True, "threshold_pct": 80, "check_minutes": 10, "alert_cooldown_hours": 6,
            "keep_backups": 3, "min_age_minutes": 5}
PROTECTED = {"state.json", "state.json.bak", "brain.json", "trades.csv", "heartbeat.json", "bot.lock",
             "telegram_pending.json", "telegram_offset.json", "risk.json", "risk_profile.json", "fx.json",
             "vybe_usage.json", "top_traders_cache.json", "antirug_state.json", "antirug_shadow.jsonl",
             "antirug_verdicts.jsonl", "bot.log", "telegram_outbox.log", "bot.out", "dash.out",
             "price_provenance.jsonl"}
ROTATED = re.compile(r"^(.+\.(log|jsonl|out))\.\d+$|^trades\.\d+\.csv$")


def settings(cfg):
    return {**DEFAULTS, **((cfg or {}).get("disk_guard") or {})}


def usage(path):
    u = shutil.disk_usage(path)
    return 100.0 * (u.total - u.free) / u.total if u.total else 0.0, u.free


def candidates(data_dir, min_age_s=300, now=None):
    """List files that may be compressed. Never raises: handles FileNotFoundError, PermissionError,
    OSError per file (e.g. a file rotated away between listdir and getmtime). This is critical for
    concurrent log rotation where another process may delete/rename the file we're scanning."""
    now = now or time.time()
    out = []
    try:
        files = os.listdir(data_dir)
    except (FileNotFoundError, PermissionError, OSError):
        return []
    for f in sorted(files):
        p = os.path.join(data_dir, f)
        if f in PROTECTED or not ROTATED.match(f):
            continue
        try:
            if not os.path.isfile(p):
                continue
            if now - os.path.getmtime(p) < min_age_s:       # still being rotated/written
                continue
        except (FileNotFoundError, PermissionError, OSError):
            continue
        out.append(p)
    return out


def _gzip(src, dst_dir):
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, f"{os.path.basename(src)}.{time.strftime('%Y%m%d-%H%M%S')}.gz")
    n = 0
    while os.path.exists(dst):
        n += 1
        dst = dst[:-3] + f"_{n}.gz"
    with open(src, "rb") as fi, gzip.open(dst + ".partial", "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo)
    with gzip.open(dst + ".partial", "rb") as chk:          # verify before removing the original
        while chk.read(1 << 20):
            pass
    os.replace(dst + ".partial", dst)
    os.remove(src)
    return dst


def compress(data_dir, keep_backups=3, min_age_s=300):
    """Returns {"freed_bytes", "files", "backups", "errors"}. Never raises."""
    arch = os.path.join(data_dir, "archive")
    res = {"freed_bytes": 0, "files": 0, "backups": 0, "errors": []}
    for p in candidates(data_dir, min_age_s):
        try:
            before = os.path.getsize(p)
            dst = _gzip(p, arch)
            res["freed_bytes"] += before - os.path.getsize(dst)
            res["files"] += 1
        except Exception as e:
            res["errors"].append(f"{os.path.basename(p)}: {e}")
    bdir = os.path.join(data_dir, "backups")
    if os.path.isdir(bdir):
        items = sorted(d for d in os.listdir(bdir) if not d.startswith(".") and os.path.isdir(os.path.join(bdir, d)))
        old = items[:-keep_backups] if keep_backups > 0 else items
        if old:
            try:
                os.makedirs(arch, exist_ok=True)
                before = sum(os.path.getsize(os.path.join(r, f)) for d in old
                             for r, _, fs in os.walk(os.path.join(bdir, d)) for f in fs)
                dst = os.path.join(arch, f"backups_{old[0]}_{old[-1]}.tar.gz")
                with tarfile.open(dst + ".partial", "w:gz") as tf:
                    for d in old:
                        tf.add(os.path.join(bdir, d), arcname=d)
                with tarfile.open(dst + ".partial", "r:gz") as tf:          # verify
                    names = tf.getnames()
                assert all(d in names for d in old)
                os.replace(dst + ".partial", dst)
                for d in old:
                    shutil.rmtree(os.path.join(bdir, d))
                res["freed_bytes"] += before - os.path.getsize(dst)
                res["backups"] = len(old)
            except Exception as e:
                res["errors"].append(f"backups: {e}")
    return res


class DiskGuard:
    def __init__(self, cfg, data_dir, notify, clock=time.time, usage_fn=usage, meta=None):
        self.s, self.data, self.notify, self.clock, self.usage = settings(cfg), data_dir, notify, clock, usage_fn
        self.meta = meta if meta is not None else {}
        self.last_check = 0.0

    def check(self, force=False):
        """Called from housekeeping; cheap. Returns the action dict when it acted, else None."""
        if not self.s["enabled"]:
            return None
        t = self.clock()
        if not force and t - self.last_check < self.s["check_minutes"] * 60:
            return None
        self.last_check = t
        pct, free = self.usage(self.data)
        if pct < self.s["threshold_pct"]:
            return None
        last = self.meta.get("disk_alert_ts")
        if not force and last is not None and t - last < self.s["alert_cooldown_hours"] * 3600:
            return None
        self.meta["disk_alert_ts"] = t
        log.warning("DISK %.1f%% used (> %s%%): compressing old data", pct, self.s["threshold_pct"])
        self.notify(f"💾 <b>Disco al {pct:.0f}%</b> (soglia {self.s['threshold_pct']}%, liberi {free / 1e9:.1f} GB).\n"
                    "Comprimo i dati vecchi (log ruotati, backup vecchi, archivi): stato, cervello e trade attuali "
                    "non vengono toccati.", "disk:alert")
        r = compress(self.data, self.s["keep_backups"], self.s["min_age_minutes"] * 60)
        pct2, free2 = self.usage(self.data)
        r.update({"used_before_pct": round(pct, 1), "used_after_pct": round(pct2, 1)})
        msg = (f"🗜 <b>Compressione completata</b>: {r['files']} file + {r['backups']} backup archiviati in data/archive, "
               f"liberati {r['freed_bytes'] / 1e6:.1f} MB. Disco ora al {pct2:.0f}% (liberi {free2 / 1e9:.1f} GB).")
        if r["errors"]:
            msg += f"\n⚠️ {len(r['errors'])} errori (vedi log)."
            log.warning("disk compress errors: %s", r["errors"][:5])
        if pct2 >= self.s["threshold_pct"]:
            msg += "\nIl disco resta sopra la soglia: libera spazio a mano (es. <code>docker system prune</code>, " \
                   "<code>journalctl --vacuum-size=200M</code>)."
        self.notify(msg, "disk:done")
        log.warning("DISK compression: freed %.1f MB, %d files, %d backups, now %.1f%%",
                    r["freed_bytes"] / 1e6, r["files"], r["backups"], pct2)
        return r
