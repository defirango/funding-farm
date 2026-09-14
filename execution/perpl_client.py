"""
REST + WebSocket client for Perpl, built from PerplFoundation/api-docs
(cloned and read directly) and its official Python/JS example scripts.

Status: matches the documented spec and the official examples field-for-
field, and rest_context()/get_markets() are exercised live (public, no
auth). The WS trading path (connect/sign-in/place/cancel) is NOT yet
proven live — that needs an enrolled API key and an on-chain exchange
account with order forwarding enabled (see README.md's "API Auth vs Smart
Contract Account" section), neither of which exists yet. Don't treat the
WS path as verified the way risex_client.py's testnet flow is.

Day-to-day trading only needs an already-issued PERPL_API_KEY +
PERPL_API_KEY_SECRET (Ed25519) — see perpl_enrollment.py for the one-time,
wallet-key-requiring alternative to just creating one at
testnet.perpl.xyz/apikeys or app.perpl.xyz/apikeys.
"""

import asyncio
import json
from typing import Optional

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from execution.perpl_signing import sign_rest_request, sign_ws_signin

MAINNET_REST_URL = "https://app.perpl.xyz/api"
MAINNET_WS_URL = "wss://app.perpl.xyz"
MAINNET_CHAIN_ID = 143

TESTNET_REST_URL = "https://testnet.perpl.xyz/api"
TESTNET_WS_URL = "wss://testnet.perpl.xyz"
TESTNET_CHAIN_ID = 10143

# OrderRequest (mt: 22) `t` field
ORDER_TYPE_OPEN_LONG = 1
ORDER_TYPE_OPEN_SHORT = 2
ORDER_TYPE_CLOSE_LONG = 3
ORDER_TYPE_CLOSE_SHORT = 4
ORDER_TYPE_CANCEL = 5
ORDER_TYPE_INCREASE_COLLATERAL = 6
ORDER_TYPE_CHANGE = 7

# OrderRequest (mt: 22) `fl` field
ORDER_FLAG_GTC = 0
ORDER_FLAG_POST_ONLY = 1
ORDER_FLAG_FOK = 2
ORDER_FLAG_IOC = 4


class PerplClient:
    """Signed REST access. Public endpoints (get_context) need no key."""

    def __init__(self, *, base_url: str = TESTNET_REST_URL, chain_id: int = TESTNET_CHAIN_ID,
                 api_key: Optional[str] = None, secret_hex: Optional[str] = None, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.chain_id = chain_id
        self.api_key = api_key
        self.priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(secret_hex.removeprefix("0x"))) \
            if secret_hex else None
        self.timeout = timeout
        self._session = requests.Session()

    def get_context(self) -> dict:
        resp = self._session.get(self.base_url + "/v1/pub/context", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_markets(self) -> list:
        return self.get_context().get("markets", [])

    def _signed_get(self, target: str) -> dict:
        if not (self.api_key and self.priv):
            raise RuntimeError("PerplClient needs api_key + secret_hex for authenticated endpoints.")
        signature, timestamp, nonce = sign_rest_request(self.priv, self.chain_id, "GET", target)
        headers = {
            "X-API-Key": self.api_key, "X-API-Timestamp": timestamp,
            "X-API-Nonce": nonce, "X-API-Signature": signature,
        }
        resp = self._session.get(self.base_url + target, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_fills(self, count: int = 20) -> dict:
        return self._signed_get(f"/v1/trading/fills?count={count}")

    def get_open_orders_history(self, count: int = 20) -> dict:
        return self._signed_get(f"/v1/trading/order-history?count={count}")


class PerplTradingSession:
    """
    One connect-trade-disconnect WS session (matches the project's
    connect-per-run architecture — no persistent connection between runs).
    NOT yet proven against a live account; see module docstring.
    """

    def __init__(self, *, ws_url: str = TESTNET_WS_URL, chain_id: int = TESTNET_CHAIN_ID,
                 api_key: str, secret_hex: str, account_id: int):
        self.ws_url = ws_url.rstrip("/") + "/ws/v1/trading"
        self.chain_id = chain_id
        self.api_key = api_key
        self.priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(secret_hex.removeprefix("0x")))
        self.account_id = account_id
        self.ws = None
        self._sn = 0
        self._rq = None  # seeded from account.lfr on connect — see docs: "strictly increasing"

    async def connect(self, timeout: float = 15.0) -> dict:
        import websockets
        self.ws = await websockets.connect(self.ws_url)
        signature, timestamp, nonce = sign_ws_signin(self.priv, self.chain_id)
        await self.ws.send(json.dumps({
            "mt": 29, "chain_id": self.chain_id, "api_key": self.api_key,
            "timestamp": timestamp, "nonce": nonce, "signature": signature,
        }))
        snapshot = await asyncio.wait_for(self._recv(), timeout)
        if snapshot.get("mt") != 19:
            raise RuntimeError(f"expected WalletSnapshot (mt=19) right after sign-in, got: {snapshot}")
        self._rq = int(snapshot.get("d", {}).get("lfr", 0) or 0)
        return snapshot

    async def _recv(self) -> dict:
        return json.loads(await self.ws.recv())

    def _next_sn(self) -> int:
        self._sn += 1
        return self._sn

    def _next_rq(self) -> int:
        self._rq += 1
        return self._rq

    async def place_order(self, *, market_id: int, order_type: int, size: int, price: int = 0,
                           leverage: int, last_exec_block: int, flags: int = ORDER_FLAG_GTC,
                           reduce_only_close: bool = False, **extra) -> tuple:
        sn = self._next_sn()
        frame = {
            "mt": 22, "sn": sn, "rq": self._next_rq(),
            "mkt": market_id, "acc": self.account_id,
            "t": order_type, "p": price, "s": size, "fl": flags,
            "lv": leverage, "lb": last_exec_block,
            **extra,
        }
        await self.ws.send(json.dumps(frame))
        return sn, frame

    async def cancel_order(self, *, market_id: int, order_id: int, last_exec_block: int) -> tuple:
        return await self.place_order(
            market_id=market_id, order_type=ORDER_TYPE_CANCEL, size=0, leverage=0,
            last_exec_block=last_exec_block, oid=order_id,
        )

    async def wait_for_status(self, sn: int, timeout: float = 15.0) -> dict:
        """mt:3 admission only — code 0 means forwarded, not filled. See wait_for_order_update."""
        while True:
            msg = await asyncio.wait_for(self._recv(), timeout)
            if msg.get("mt") == 3 and msg.get("cid") == sn:
                return msg

    async def close(self):
        if self.ws is not None:
            await self.ws.close()
