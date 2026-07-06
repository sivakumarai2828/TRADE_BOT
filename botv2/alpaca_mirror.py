"""Optional Alpaca paper mirror for US trades.

Design: the local PaperLedger is always the source of truth (one code path,
one journal, works for both markets). If Alpaca keys are set, every US fill
is mirrored to the Alpaca paper account so you can also watch it there.
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger("botv2.alpaca")


class AlpacaMirror:
    def __init__(self, key: str, secret: str, base_url: str):
        self.enabled = bool(key and secret)
        self.base = base_url.rstrip("/")
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    def order(self, symbol: str, qty: float, side: str) -> None:
        if not self.enabled:
            return
        try:
            r = requests.post(
                f"{self.base}/v2/orders",
                headers=self.headers,
                json={"symbol": symbol, "qty": str(int(qty)), "side": side,
                      "type": "market", "time_in_force": "day"},
                timeout=10,
            )
            if r.status_code >= 300:
                log.warning("alpaca mirror %s %s failed: %s", side, symbol, r.text[:200])
        except Exception as exc:
            log.warning("alpaca mirror error: %s", exc)
