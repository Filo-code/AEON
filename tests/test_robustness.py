#!/usr/bin/env python3
"""Offline robustness tests (no real network needed):
 1. hard total HTTP deadline against a server that trickles bytes forever
 2. watchdog: a hung main loop makes the process exit (code 3) so Docker/systemd restart it
"""
import os, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))


class Trickle(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        try:
            for _ in range(600):           # 1 byte per second for 10 minutes
                self.wfile.write(b" "); self.wfile.flush(); time.sleep(1)
        except Exception:
            pass

    def log_message(self, *a):
        pass


def test_deadline():
    from paper_bot import Http
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    h = Http(total_timeout=4, connect_timeout=2, read_timeout=3)
    t0 = time.time()
    r = h.get(f"http://127.0.0.1:{srv.server_port}/x", tries=1)
    el = time.time() - t0
    ok = r is None and el < 8
    print(f"[1] trickling server: call abandoned after {el:.1f}s (total deadline 4s; plain read timeout would wait 10 min) -> {'OK' if ok else 'FAIL'}")
    return ok


HANG = (
    "import os, sys, time, yaml, logging\n"
    'sys.path.insert(0, os.environ["EZREKT_BOT_PATH"])\n'
    "import paper_bot\n"
    'logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")\n'
    'cfg = yaml.safe_load(open(os.environ["EZREKT_CONFIG_PATH"]))\n'
    'cfg["watchdog_seconds"] = 15\n'
    'cfg["run_minutes"] = 0\n'
    'b = paper_bot.PaperBot(cfg, os.environ["EZREKT_DATA_DIR"], reset=True)\n'
    "b.tg.dry = True\n"
    "def stuck(): time.sleep(600)\n"
    "b.discover = stuck\n"
    "b.run()\n"
)


def test_watchdog():
    d = tempfile.mkdtemp()
    try:
        bot_path = os.path.join(ROOT, "bot")
        cfg_path = os.path.join(ROOT, "bot", "config.yaml")
        env = {
            **os.environ,
            "EZREKT_BOT_PATH": bot_path,
            "EZREKT_CONFIG_PATH": cfg_path,
            "EZREKT_DATA_DIR": d,
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
            "VYBE_API_KEY": "",
        }
        t0 = time.time()
        p = subprocess.run(
            [sys.executable, "-c", HANG],
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
        )
        el = time.time() - t0
        ok = (
            p.returncode == 3
            and "WATCHDOG" in p.stdout + p.stderr
            and os.path.exists(os.path.join(d, "state.json"))
        )
        print(f"[2] hung loop: process exited with code {p.returncode} after {el:.0f}s, state.json saved -> {'OK' if ok else 'FAIL'}")
        if not ok:
            print(f"  stdout: {p.stdout[-500:]}")
            print(f"  stderr: {p.stderr[-500:]}")
        return ok
    finally:
        # Cleanup temp directory
        try:
            import shutil
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(0 if all([test_deadline(), test_watchdog()]) else 1)
