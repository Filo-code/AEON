#!/usr/bin/env python3
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

os.environ.update({
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "VYBE_API_KEY": "",
    "LOG_STDOUT": "0",
})

import paper_bot


class FakeHttp:
    def __init__(self, data):
        self.data = data

    def request_ex(self, *args, **kwargs):
        return SimpleNamespace(status=200, data=self.data, kind="ok")


def run_case(name, endpoint, response, expected_route):
    cfg = {
        "endpoints": [endpoint],
        "request_deadline_seconds": 2.5,
        "cache_seconds": 0,
    }
    jup = paper_bot.Jupiter(FakeHttp(response), cfg)
    quote = jup.quote("INPUT", "OUTPUT", 1000)

    assert quote is not None, f"{name}: quote missing"
    assert quote["route"] == expected_route, (
        f"{name}: expected route {expected_route!r}, got {quote['route']!r}"
    )
    assert paper_bot.PaperBot._is_valid_jupiter_route(quote["route"]) == bool(expected_route)
    print(f"PASS {name}: route={quote['route']!r}")


run_case(
    "Jupiter V2 router",
    "https://api.jup.ag/swap/v2/order",
    {"outAmount": "2000", "router": "metis"},
    "metis",
)

run_case(
    "Jupiter V1 routePlan",
    "https://api.jup.ag/swap/v1/quote",
    {
        "outAmount": "2000",
        "routePlan": [{"swapInfo": {"label": "Raydium"}}],
    },
    "Raydium",
)

run_case(
    "Missing route remains invalid",
    "https://api.jup.ag/swap/v2/order",
    {"outAmount": "2000"},
    "",
)
