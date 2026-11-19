#!/usr/bin/env python3
"""Docker HEALTHCHECK for the bot container.
healthy  = heartbeat.json updated in the last HEALTH_MAX_AGE s (default 180) and status running|dead.
If the heartbeat is older than HEALTH_KILL_AGE (default 420 s, i.e. the internal watchdog (240 s) also failed) the
bot process (PID 1's child) is sent SIGTERM then SIGKILL: the container exits and `restart: unless-stopped`
restarts it. This is the EFFECTIVE recovery: plain Docker never restarts an 'unhealthy' container by itself."""
import json, os, signal, sys, time

D = os.getenv("DATA_DIR", "/app/data")
MAX_AGE = float(os.getenv("HEALTH_MAX_AGE", "180"))
KILL_AGE = float(os.getenv("HEALTH_KILL_AGE", "420"))
try:
    hb = json.load(open(os.path.join(D, "heartbeat.json")))
except Exception:
    try:
        hb = {"ts": json.load(open(os.path.join(D, "state.json"))).get("heartbeat_ts", 0), "pid": None}
    except Exception:
        print("no heartbeat yet"); sys.exit(1)
age = time.time() - float(hb.get("ts") or 0)
if age < MAX_AGE:
    print(f"ok heartbeat {age:.0f}s"); sys.exit(0)
print(f"STALE heartbeat {age:.0f}s")
if age > KILL_AGE and hb.get("pid"):
    pid = int(hb["pid"])
    try:
        os.kill(pid, signal.SIGTERM); time.sleep(20); os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError as e:
        print("cannot kill:", e)
sys.exit(1)
