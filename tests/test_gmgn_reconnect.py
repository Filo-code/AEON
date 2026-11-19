#!/usr/bin/env python3
import os, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
from gmgn import GmGnScanner

with tempfile.TemporaryDirectory() as d:
    s = GmGnScanner(d, enabled=True)
    s._stable_session = False
    s._subscription_ack_ts = time.monotonic() - 61
    s._mark_session_stable()
    assert s._stable_session is True
    assert s._subscription_ack_ts is None

    s._stable_session = False
    s._subscription_ack_ts = time.monotonic() - 10
    s._mark_session_stable()
    assert s._stable_session is False

    s._subscription_ack_ts = None
    s._mark_session_stable()
    assert s._stable_session is False

print("GMGN reconnect tests: 3 passed")
