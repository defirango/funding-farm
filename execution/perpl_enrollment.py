"""
Programmatic Ed25519 API key enrollment for Perpl. Perpl's own docs and
example scripts say most users don't need this: create a key directly at
testnet.perpl.xyz/apikeys or app.perpl.xyz/apikeys (connect wallet, click
create) and skip this file entirely. This exists for completeness and
because it needs the MAIN WALLET key exactly once, same pattern as
risex_registration.py — never held by the bot's day-to-day runtime
(perpl_client.py only needs the resulting API_KEY + Ed25519 secret).

NOT YET PROVEN LIVE: /v1/api-key/payload and /v1/api-key/enroll require a
Perpl-whitelisted Origin header (per integrations.md) — an external
dependency (ask Perpl to whitelist one) this can't satisfy on its own. The
web-UI path above sidesteps this entirely and is the one Perpl itself
recommends for a single user's own key.
"""

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from execution.perpl_client import MAINNET_CHAIN_ID, MAINNET_REST_URL
from execution.perpl_signing import sign_enrollment_payload

SCOPE_READ = 1
SCOPE_TRADE = 2  # implies read
SCOPE_BOTH = 3


def generate_key_pair():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes_raw()
    return priv, "0x" + pub.hex()


def enroll_api_key(wallet_account, *, base_url: str = MAINNET_REST_URL, chain_id: int = MAINNET_CHAIN_ID,
                    origin: str, label: str, scope_mask: int = SCOPE_BOTH, timeout: float = 15.0) -> dict:
    """
    wallet_account: eth_account LocalAccount for the account's own wallet —
    needed only for this call. origin: MUST already be whitelisted by Perpl
    (see module docstring) or /v1/api-key/payload rejects the request.

    Returns {"api_key": str, "secret_hex": str, "info": <ApiKeyInfo dict>}.
    """
    priv, public_key_hex = generate_key_pair()
    headers = {"Content-Type": "application/json", "Origin": origin}

    payload_resp = requests.post(
        base_url.rstrip("/") + "/v1/api-key/payload",
        headers=headers, timeout=timeout,
        json={
            "chain_id": chain_id, "address": wallet_account.address,
            "public_key": public_key_hex, "scope_mask": scope_mask, "label": label,
        },
    )
    payload_resp.raise_for_status()
    payload = payload_resp.json()
    typed_data, mac = payload["typed_data"], payload["mac"]

    wallet_signature_hex, pop_signature_hex = sign_enrollment_payload(wallet_account, priv, typed_data)

    enroll_resp = requests.post(
        base_url.rstrip("/") + "/v1/api-key/enroll",
        headers=headers, timeout=timeout,
        json={
            "chain_id": chain_id, "address": wallet_account.address,
            "typed_data": typed_data, "mac": mac,
            "signature": wallet_signature_hex, "pop_signature": pop_signature_hex,
        },
    )
    enroll_resp.raise_for_status()
    info = enroll_resp.json()["api_key"]

    secret_hex = "0x" + priv.private_bytes_raw().hex()
    return {"api_key": info["api_key"], "secret_hex": secret_hex, "info": info}
