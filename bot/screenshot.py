"""Hourly dashboard screenshot to Telegram (paper bot; works on the VPS too).

How: a headless Chromium/Chrome (command line, no Python dependency) opens the dashboard through a tiny AUTH PROXY
bound to 127.0.0.1 on a random port, alive only for the few seconds of the capture. The proxy adds the Basic-Auth
header itself, so the dashboard password never appears in a URL, a command line (`ps`), a log or a message.
No browser / capture failure -> a short TEXT summary is sent instead (never breaks the bot).
"""
import base64
import http.client
import http.server
import logging
import os
import shutil
import sys
import subprocess
import tempfile
import threading
import time
import urllib.parse

log = logging.getLogger("paperbot")

BROWSERS = ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome", "chrome")
DEFAULTS = {"hourly_screenshot": True, "screenshot_interval_minutes": 60, "screenshot_first_delay_s": 120,
            "screenshot_width": 1280, "screenshot_height": 2400, "screenshot_timeout_s": 60,
            "screenshot_virtual_time_ms": 12000, "screenshot_browser": "", "screenshot_url": ""}


def settings(cfg):
    t = dict(DEFAULTS)
    t.update({k: v for k, v in ((cfg or {}).get("telegram") or {}).items() if k in DEFAULTS})
    return t


def find_browser(path=""):
    """Explicit path (config or env SCREENSHOT_BROWSER) or the first browser found in PATH; None if none."""
    p = (path or os.getenv("SCREENSHOT_BROWSER", "")).strip()
    if p:
        return p if (os.path.isfile(p) and os.access(p, os.X_OK)) or shutil.which(p) else None
    for b in BROWSERS:
        w = shutil.which(b)
        if w:
            return w
    return None


def target_url(st):
    """Dashboard base URL as seen FROM THE BOT: config, env DASHBOARD_SCREENSHOT_URL (docker: http://dashboard:8080),
    else http://127.0.0.1:$PORT. (DASHBOARD_URL is the public link for humans and is not used here.)"""
    u = (st.get("screenshot_url") or os.getenv("DASHBOARD_SCREENSHOT_URL", "")).strip()
    return (u or f"http://127.0.0.1:{os.getenv('PORT', '8080')}").rstrip("/")


class _Proxy(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_auth_proxy(target, user, password):
    """Local GET-only proxy 127.0.0.1:<random> -> target, adding Authorization. Returns (server, base_url)."""
    tu = urllib.parse.urlsplit(target)
    auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode() if password else None
    conn_cls = http.client.HTTPSConnection if tu.scheme == "https" else http.client.HTTPConnection

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                c = conn_cls(tu.hostname, tu.port or (443 if tu.scheme == "https" else 80), timeout=20)
                hdr = {"Accept": self.headers.get("Accept", "*/*"), "Accept-Encoding": "identity"}
                if auth:
                    hdr["Authorization"] = auth
                c.request("GET", (tu.path.rstrip("/") + self.path) or "/", headers=hdr)
                r = c.getresponse()
                body = r.read()
                self.send_response(r.status)
                for k, v in r.getheaders():
                    if k.lower() in ("content-type", "cache-control"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                c.close()
            except Exception as e:                       # never echo headers (they hold the password)
                try:
                    self.send_error(502, type(e).__name__)
                except Exception:
                    pass

        def do_POST(self):                               # read-only: the screenshot never changes anything
            self.send_error(405)

        def log_message(self, *a):                       # silent: no request lines in the bot log
            pass

    srv = _Proxy(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, name="shot-proxy", daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/"


def _no_sandbox():
    if os.getenv("SCREENSHOT_NO_SANDBOX", "").strip().lower() in ("1", "true", "yes"):
        return True
    return os.path.exists("/.dockerenv") or (hasattr(os, "geteuid") and os.geteuid() == 0)


class _Pipe:
    """Chrome DevTools protocol over --remote-debugging-pipe (fd 3 = to Chrome, fd 4 = from Chrome; JSON + NUL).
    Standard library only: no websocket / playwright dependency."""

    def __init__(self, proc, wfd, rfd, deadline):
        self.p, self.w, self.r, self.deadline = proc, wfd, rfd, deadline
        self.buf, self.n, self.events = b"", 0, []

    def _read_msg(self):
        import json, select
        while b"\0" not in self.buf:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("devtools")
            rd, _, _ = select.select([self.r], [], [], min(left, 1.0))
            if rd:
                chunk = os.read(self.r, 1 << 20)
                if not chunk:
                    raise EOFError("browser closed")
                self.buf += chunk
            elif self.p.poll() is not None:
                raise EOFError(f"browser exit {self.p.returncode}")
        msg, self.buf = self.buf.split(b"\0", 1)
        return json.loads(msg)

    def call(self, method, params=None, session=None):
        import json
        self.n += 1
        m = {"id": self.n, "method": method, "params": params or {}}
        if session:
            m["sessionId"] = session
        os.write(self.w, json.dumps(m).encode() + b"\0")
        while True:
            d = self._read_msg()
            if d.get("id") == self.n:
                if "error" in d:
                    raise RuntimeError(f"{method}: {str(d['error'].get('message', ''))[:80]}")
                return d.get("result") or {}
            self.events.append(d)

    def wait_event(self, name, timeout):
        end = min(self.deadline, time.monotonic() + timeout)
        while True:
            for i, e in enumerate(self.events):
                if e.get("method") == name:
                    return self.events.pop(i)
            if time.monotonic() >= end:
                return None
            saved, self.deadline = self.deadline, end
            try:
                self.events.append(self._read_msg())
            except TimeoutError:
                return None
            finally:
                self.deadline = saved


def _cdp_capture(browser, url, out_path, st, prof):
    import base64 as b64
    r3, w3 = os.pipe()        # bot -> chrome (chrome reads fd 3)
    r4, w4 = os.pipe()        # chrome -> bot (chrome writes fd 4)
    args = [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions", "--disable-dev-shm-usage", "--mute-audio",
            "--remote-debugging-pipe", f"--user-data-dir={prof}"] + (["--no-sandbox"] if _no_sandbox() else []) \
        + ["about:blank"]

    launcher = (
        "import os,sys;"
        "r3,w4=map(int,sys.argv[1:3]);"
        "args=sys.argv[3:];"
        "os.dup2(r3,3);os.dup2(w4,4);" "os.set_inheritable(3,True);os.set_inheritable(4,True);"
        "[os.close(fd) for fd in (r3,w4) if fd not in (3,4)];"
        "os.execv(args[0],args)"
    )

    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", launcher, str(r3), str(w4), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(r3, w4)
        )
    except (OSError, FileNotFoundError) as e:
        log.warning("screenshot Popen fallito (%s): browser mancante o permessi", e)
        raise
    finally:
        # Close our ends of the pipes in the parent, even if Popen raised: otherwise the read/write ends leak
        # when the browser is missing or fails to start.
        try:
            os.close(r3)
        except OSError:
            pass
        try:
            os.close(w4)
        except OSError:
            pass

    cdp = _Pipe(proc, w3, r4, time.monotonic() + float(st["screenshot_timeout_s"]))
    try:
        tid = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        sid = cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        w, h = int(st["screenshot_width"]), int(st["screenshot_height"])
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1,
                                                         "mobile": False}, sid)
        cdp.call("Page.enable", {}, sid)
        cdp.call("Page.navigate", {"url": url}, sid)
        if cdp.wait_event("Page.loadEventFired", 20) is None:
            log.warning("screenshot: Page.loadEventFired timeout (dashboard potrebbe essere stato reindirizzato)")
        time.sleep(min(8.0, float(st["screenshot_virtual_time_ms"]) / 1000.0))   # dashboard JS fetches /api/state
        try:                                                                     # whole page, capped
            ch = cdp.call("Page.getLayoutMetrics", {}, sid).get("cssContentSize", {}).get("height")
            if ch:
                h2 = int(min(max(float(ch), 600), 2 * h))
                cdp.call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h2, "deviceScaleFactor": 1,
                                                                 "mobile": False}, sid)
                time.sleep(0.5)
        except Exception:
            pass
        data = cdp.call("Page.captureScreenshot", {"format": "png"}, sid)["data"]
        with open(out_path, "wb") as f:
            f.write(b64.b64decode(data))
        try:
            cdp.call("Browser.close")
        except Exception:
            pass
    finally:
        for fd in (w3, r4):
            try:
                os.close(fd)
            except OSError:
                pass
        if proc:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def capture(st, out_path, user=None, password=None, cdp=None):
    """Returns (ok, reason). Never raises; never puts the password on a command line."""
    browser = find_browser(st.get("screenshot_browser"))
    if not browser:
        return False, "browser non installato (chromium)"
    user = user if user is not None else os.getenv("DASHBOARD_USER", "admin")
    password = password if password is not None else os.getenv("DASHBOARD_PASSWORD", "")
    srv = None
    data_dir = os.getenv("DATA_DIR", "/app/data")
    os.makedirs(data_dir, exist_ok=True)
    prof = os.getenv("CHROMIUM_PROFILE_DIR", os.path.join(data_dir, "chromium-profile"))
    os.makedirs(prof, exist_ok=True)

    lock_fd = open(os.path.join(data_dir, "chromium-profile.lock"), "a")
    try:
        import fcntl
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fd.close()
        return False, "cattura Chromium già in corso"
    try:
        srv, url = start_auth_proxy(target_url(st), user, password)
        try:
            os.remove(out_path)
        except FileNotFoundError:
            pass
        (cdp or _cdp_capture)(browser, url, out_path, st, prof)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            return True, "ok"
        return False, "screenshot vuoto"
    except TimeoutError:
        return False, f"browser oltre {st['screenshot_timeout_s']} s"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:100]}"
    finally:
        if srv:
            srv.shutdown()
            srv.server_close()
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        finally:
            lock_fd.close()


def safe_text(s, secrets):
    """Strip any secret value from a text before it reaches a log or a message (defence in depth)."""
    s = str(s)
    for v in secrets:
        if v and len(v) >= 4:
            s = s.replace(v, "***")
    return s
