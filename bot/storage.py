"""Persistence and host-level helpers: crash-safe JSON, single-instance lock, backups with retention,
disk-space check, size-capped text logs, systemd sd_notify (no extra dependency)."""
import errno, json, logging, os, shutil, socket, threading, time
try:
    import fcntl  # Unix-only file lock
except ImportError:
    fcntl = None
try:
    import msvcrt  # Windows byte-range lock (released automatically on process exit)
except ImportError:
    msvcrt = None
import sys

log = logging.getLogger("paperbot.storage")
_PATH_LOCKS = {}
_PL_GUARD = threading.Lock()


def _path_lock(path):
    with _PL_GUARD:
        return _PATH_LOCKS.setdefault(os.path.abspath(path), threading.Lock())


def _fsync_dir(d):
    try:
        fd = os.open(d or ".", os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def atomic_json(path, obj, keep_bak=False, indent=1, lock_timeout=10.0):
    """Crash-safe write: unique tmp file in the same dir -> fsync -> rename -> fsync dir.
    A crash (or a full disk) at ANY point leaves either the old or the new file, never a partial one.
    Concurrent writers to the same path are serialised. keep_bak=True keeps the previous good copy as path.bak.
    Raises OSError on failure (e.g. ENOSPC) - the old file is untouched."""
    lk = _path_lock(path)
    if not lk.acquire(timeout=lock_timeout):
        raise TimeoutError(f"write lock busy for {path}")
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        data = json.dumps(obj, indent=indent, default=str, allow_nan=False)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if keep_bak and os.path.exists(path):
            try:
                os.replace(path, path + ".bak")       # previous good version
            except OSError:
                pass
        os.replace(tmp, path)
        _fsync_dir(os.path.dirname(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    finally:
        lk.release()


def sanitize(obj):
    """Replace NaN/inf (not valid JSON) by None, recursively."""
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    return obj


def load_json_safe(path, validate=None, backup_dir=None, backup_name=None):
    """Load path; if missing -> (None, 'missing'). If corrupt/invalid -> try path.bak, then the newest backups.
    Returns (obj, source) where source is 'main' | 'bak' | 'backup:<dir>' | 'missing' | 'corrupt'."""
    validate = validate or (lambda o: isinstance(o, dict))
    cands = [(path, "main"), (path + ".bak", "bak")]
    if backup_dir and backup_name and os.path.isdir(backup_dir):
        for d in sorted(os.listdir(backup_dir), reverse=True):
            cands.append((os.path.join(backup_dir, d, backup_name), "backup:" + d))
    # Do not return early when main and .bak are absent: automatic snapshots
    # may still contain a recoverable copy.
    for p, src in cands:
        try:
            with open(p, encoding="utf-8") as fh:
                o = json.load(fh)
            if validate(o):
                if src != "main":
                    log.error("%s unreadable or invalid: recovered from %s", os.path.basename(path), src)
                return o, src
            log.error("%s (%s) failed validation", p, src)
        except FileNotFoundError:
            continue
        except Exception as e:
            log.error("cannot read %s (%s): %s", p, src, e)
    return None, "corrupt"


class InstanceLock:
    """Exclusive OS-managed lock on DATA_DIR/bot.lock.

    Unix uses flock; Windows uses a one-byte msvcrt lock. Both are released by
    the OS when a process exits/crashes, so a stale PID/file never blocks restart.
    The lock file is intentionally retained to avoid unlink/recreate races.
    """

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "bot.lock")
        self.fh = None
        self._is_windows = sys.platform.startswith("win")
        self._locked = False

    def acquire(self):
        try:
            self.fh = open(self.path, "a+")
            if self._is_windows:
                if msvcrt is None:
                    raise OSError("Windows locking API unavailable")
                self.fh.seek(0, os.SEEK_END)
                if self.fh.tell() == 0:
                    self.fh.write(" ")
                    self.fh.flush()
                self.fh.seek(0)
                try:
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    self.fh.close()
                    self.fh = None
                    return False
            else:
                if fcntl is None:
                    raise OSError("Unix flock API unavailable")
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._locked = True
            self.fh.seek(0)
            self.fh.truncate()
            self.fh.write(str(os.getpid()))
            self.fh.flush()
            os.fsync(self.fh.fileno())
            return True
        except OSError as e:
            log.warning("cannot acquire instance lock %s: %s", self.path, e)
            if self.fh:
                try:
                    self.fh.close()
                except OSError:
                    pass
            self.fh = None
            self._locked = False
            return False

    def release(self):
        if not self.fh:
            return
        try:
            if self._locked:
                self.fh.seek(0)
                if self._is_windows and msvcrt:
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                elif fcntl:
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            log.exception("cannot release instance lock %s", self.path)
        finally:
            try:
                self.fh.close()
            except OSError:
                pass
            self.fh = None
            self._locked = False


def disk_status(path, warn_mb=300, critical_mb=100):
    try:
        u = shutil.disk_usage(path)
    except OSError as e:
        return {"ok": False, "level": "unknown", "error": str(e)}
    free_mb = u.free / 1e6
    level = "critical" if free_mb < critical_mb else "warn" if free_mb < warn_mb else "ok"
    return {"ok": level == "ok", "level": level, "free_mb": round(free_mb), "total_mb": round(u.total / 1e6),
            "used_pct": round(100 * (u.total - u.free) / u.total, 1) if u.total else None}


def dir_size_mb(path):
    tot = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                tot += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return tot / 1e6


class Backups:
    """Snapshot of the important files into DATA_DIR/backups/<YYYYmmdd-HHMMSS>/ with retention."""
    FILES = ("state.json", "brain.json", "trades.csv", "telegram_offset.json", "vybe_usage.json", "risk.json")

    def __init__(self, data_dir, keep=48, min_free_mb=200):
        self.data = data_dir
        self.dir = os.path.join(data_dir, "backups")
        self.keep, self.min_free_mb = int(keep), min_free_mb

    def run(self):
        """Returns dict with outcome. Never raises."""
        try:
            ds = disk_status(self.data)
            if ds.get("free_mb") is not None and ds["free_mb"] < self.min_free_mb:
                return {"ok": False, "error": f"spazio disco insufficiente ({ds['free_mb']} MB liberi)"}
            os.makedirs(self.dir, exist_ok=True)
            name = time.strftime("%Y%m%d-%H%M%S")
            tmp = os.path.join(self.dir, "." + name + ".partial")
            os.makedirs(tmp, exist_ok=True)
            n = 0
            for f in self.FILES:
                src = os.path.join(self.data, f)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(tmp, f)); n += 1
            # Validate every copied JSON artifact, not only state.json. A backup
            # containing a truncated/corrupt auxiliary JSON file is not complete.
            for copied_name in os.listdir(tmp):
                if copied_name.lower().endswith(".json"):
                    copied_path = os.path.join(tmp, copied_name)
                    with open(copied_path, encoding="utf-8") as fh:
                        json.load(fh)
            final = os.path.join(self.dir, name)
            os.replace(tmp, final)
            removed = self.prune()
            return {"ok": True, "dir": final, "files": n, "removed": removed, "count": len(self.list())}
        except Exception as e:
            log.error("backup failed: %s", e)
            for d in os.listdir(self.dir) if os.path.isdir(self.dir) else []:
                if d.endswith(".partial"):
                    shutil.rmtree(os.path.join(self.dir, d), ignore_errors=True)
            return {"ok": False, "error": str(e)[:200]}

    def list(self):
        if not os.path.isdir(self.dir):
            return []
        return sorted(d for d in os.listdir(self.dir) if not d.startswith("."))

    def prune(self):
        items = self.list()
        old = items[:-self.keep] if self.keep > 0 else []
        for d in old:
            shutil.rmtree(os.path.join(self.dir, d), ignore_errors=True)
        return len(old)


class CappedLog:
    """Append-only text log with a size cap (rotates to .1); never raises."""

    def __init__(self, path, max_bytes=2_000_000, backups=1):
        self.path, self.max, self.backups = path, max_bytes, backups
        self.lock = threading.Lock()
        self.enabled = True

    def write(self, text):
        if not self.path or not self.enabled:
            return
        with self.lock:
            try:
                if os.path.exists(self.path) and os.path.getsize(self.path) > self.max:
                    for i in range(self.backups, 0, -1):
                        src = self.path if i == 1 else f"{self.path}.{i - 1}"
                        if os.path.exists(src):
                            os.replace(src, f"{self.path}.{i}")
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(text)
            except OSError as e:
                if e.errno == errno.ENOSPC:
                    log.error("disk full: %s not written", os.path.basename(self.path))


def sd_notify(msg):
    """systemd notify protocol (Type=notify + WatchdogSec). No-op outside systemd."""
    addr = os.getenv("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
        return True
    except OSError:
        return False
