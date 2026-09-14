"""
REST wrapper around the RISEx trading API. Day-to-day trading only ever
needs the SIGNER (session key) — construct this with the signer only, never
the main account's private key. See risex_registration.py for the one-time
registration step, which is the only place the main account key is needed.
"""

from typing import Optional

import requests

from execution.risex_signing import (
    NonceTracker,
    build_permit,
    cancel_all_orders_action_hash,
    cancel_order_action_hash,
    pack_order_uint88,
    place_order_action_hash,
)

MAINNET_BASE_URL = "https://api.rise.trade"
TESTNET_BASE_URL = "https://api.testnet.rise.trade"


def _unwrap(payload):
    return payload.get("data", payload) if isinstance(payload, dict) else payload


class RiseXClient:
    def __init__(self, *, base_url: str, account_address: str, signer, allow_mainnet: bool = False,
                 timeout: float = 15.0):
        if base_url == MAINNET_BASE_URL and not allow_mainnet:
            raise RuntimeError(
                "Refusing to construct a RiseXClient against mainnet (allow_mainnet=False). "
                "Still in the testnet-proving phase per the rollout plan."
            )
        self.base_url = base_url.rstrip("/")
        self.account_address = account_address
        self.signer = signer  # eth_account LocalAccount — the session key only
        self.timeout = timeout
        self._session = requests.Session()
        self._domain: Optional[dict] = None
        self._router: Optional[str] = None
        self._nonce_tracker: Optional[NonceTracker] = None

    # -- low-level HTTP ---------------------------------------------------
    def _get(self, path: str, params: dict = None):
        resp = self._session.get(self.base_url + path, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict):
        resp = self._session.post(self.base_url + path, json=body, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # -- domain / router / nonce -------------------------------------------
    def domain(self) -> dict:
        if self._domain is None:
            from eth_utils import to_checksum_address
            d = _unwrap(self._get("/v1/auth/eip712-domain"))
            self._domain = {
                "name": d["name"],
                "version": d["version"],
                "chainId": int(d["chain_id"]),
                "verifyingContract": to_checksum_address(d["verifying_contract"]),
            }
        return self._domain

    def router_address(self) -> str:
        if self._router is None:
            from eth_utils import to_checksum_address
            cfg = _unwrap(self._get("/v1/system/config"))
            self._router = to_checksum_address(cfg["addresses"]["router"])
        return self._router

    def nonce_state(self) -> dict:
        return _unwrap(self._get(f"/v1/nonce-state/{self.account_address}"))

    def _tracker(self) -> NonceTracker:
        if self._nonce_tracker is None:
            current_anchor = int(self.nonce_state()["nonce_anchor"])
            self._nonce_tracker = NonceTracker(current_anchor)
        return self._nonce_tracker

    def _permit(self, action_hash: bytes) -> dict:
        anchor, bit = self._tracker().next()
        return build_permit(
            self.signer, account_address=self.account_address, domain=self.domain(),
            router_address=self.router_address(), action_hash=action_hash,
            nonce_anchor=anchor, nonce_bitmap_index=bit,
        )

    # -- markets / orders (read) --------------------------------------------
    def get_markets(self) -> list:
        return _unwrap(self._get("/v1/markets"))["markets"]

    def get_open_orders(self, market_id: Optional[int] = None) -> list:
        params = {"account": self.account_address}
        if market_id is not None:
            params["market_id"] = market_id
        return _unwrap(self._get("/v1/orders/open", params=params))["orders"]

    def session_key_status(self) -> dict:
        return _unwrap(self._get(
            "/v1/auth/session-key-status",
            params={"account": self.account_address, "signer": self.signer.address},
        ))

    # -- trading (write) ----------------------------------------------------
    def place_order(self, *, market_id: int, size_steps: int, price_ticks: int, side: int,
                     post_only: bool, reduce_only: bool, stp_mode: int, order_type: int,
                     time_in_force: int) -> dict:
        order_data = pack_order_uint88(
            market_id=market_id, size_steps=size_steps, price_ticks=price_ticks, side=side,
            post_only=post_only, reduce_only=reduce_only, stp_mode=stp_mode,
            order_type=order_type, time_in_force=time_in_force,
        )
        action_hash = place_order_action_hash(order_data)
        body = {
            "market_id": market_id, "size_steps": size_steps, "price_ticks": price_ticks,
            "side": side, "post_only": post_only, "reduce_only": reduce_only,
            "stp_mode": stp_mode, "order_type": order_type, "time_in_force": time_in_force,
            "permit": self._permit(action_hash),
        }
        return _unwrap(self._post("/v1/orders/place", body))

    def cancel_order(self, *, market_id: int, order_id: str, resting_order_id: int) -> dict:
        action_hash = cancel_order_action_hash(market_id, resting_order_id)
        body = {"market_id": market_id, "order_id": order_id, "permit": self._permit(action_hash)}
        return _unwrap(self._post("/v1/orders/cancel", body))

    def cancel_all_orders(self):
        cancel_all_orders_action_hash()  # always raises — see risex_signing.py

    # -- testnet only ---------------------------------------------------------
    def faucet_deposit(self, amount: str = "1000") -> dict:
        if self.base_url != TESTNET_BASE_URL:
            raise RuntimeError("faucet_deposit() only works against the testnet base URL.")
        return _unwrap(self._post("/v1/account/deposit", {"account": self.account_address, "amount": amount}))
