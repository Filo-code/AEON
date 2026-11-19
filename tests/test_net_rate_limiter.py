import json
import threading
import time

from bot.net import Http, Resp, RATE, TIMEOUT, DEFERRED


HOST = "test.example"


def test_spacing_deadline_is_deferred_not_provider_failure():
    h = Http(breaker_after=1)
    h.next_allowed[HOST] = time.monotonic() + 2

    r = h.request_ex(f"https://{HOST}/test", tries=1, deadline=0.05)

    assert r.kind == DEFERRED
    assert h.health[HOST].consec == 0
    assert HOST not in h.err
    assert h.health[HOST].open_until == 0


class Fake429:
    status_code = 429
    headers = {}

    def close(self):
        pass


class Fake429Session:
    def __init__(self):
        self.headers = {}

    def get(self, *args, **kwargs):
        return Fake429()


def test_http_429_does_not_open_circuit(monkeypatch):
    monkeypatch.setitem(Http.SPACING, HOST, 0)

    h = Http(breaker_after=1)
    h.s = Fake429Session()

    r = h.request_ex(f"https://{HOST}/test", tries=1, deadline=1)

    assert r.kind == RATE
    assert h.health[HOST].consec == 0
    assert h.health[HOST].open_until == 0
    assert HOST in h.cooldown


class FakeRaw:
    def __init__(self):
        self.sent = False

    def read1(self, size, decode_content=True):
        if not self.sent:
            self.sent = True
            return b'{}'
        return b''


class Fake200:
    status_code = 200
    headers = {}

    def __init__(self):
        self.raw = FakeRaw()

    def close(self):
        pass


class Fake200Session:
    def __init__(self):
        self.headers = {}
        self.lock = threading.Lock()
        self.starts = []

    def get(self, *args, **kwargs):
        with self.lock:
            self.starts.append(time.monotonic())
        return Fake200()


def test_concurrent_requests_respect_host_spacing(monkeypatch):
    monkeypatch.setitem(Http.SPACING, HOST, 0.12)

    h = Http()
    fake = Fake200Session()
    h.s = fake

    results = []

    def call():
        results.append(
            h.request_ex(f"https://{HOST}/test", tries=1, deadline=2)
        )

    t1 = threading.Thread(target=call)
    t2 = threading.Thread(target=call)

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(fake.starts) == 2
    assert all(r.kind == "ok" for r in results)
    assert abs(fake.starts[0] - fake.starts[1]) >= 0.10


def test_real_timeout_still_opens_circuit(monkeypatch):
    monkeypatch.setitem(Http.SPACING, HOST, 0)

    h = Http(breaker_after=1)

    def fake_attempt(*args, **kwargs):
        return Resp(kind=TIMEOUT, error="simulated real timeout")

    h._attempt = fake_attempt

    r = h.request_ex(f"https://{HOST}/test", tries=1, deadline=1)

    assert r.kind == TIMEOUT
    assert h.health[HOST].consec == 1
    assert h.health[HOST].open_until > time.time()
