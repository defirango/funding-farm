"""
Pure crypto for Perpl: Ed25519 REST/WS request signing and the EIP-712
key-enrollment payload signing. Ported from Perpl's own official example
scripts (github.com/PerplFoundation/api-docs/examples/{python,js}) rather
than reconstructed from docs prose — imad's brief noted a websearch summary
of these docs got a construct detail wrong before (WS auth message type),
so those executable examples are the source of truth here, cloned and read
directly.

No network calls in this file — see perpl_client.py for the REST/WS wrapper.
"""

import base64
import hashlib
import secrets
import time


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def rest_canonical_string(chain_id: int, method: str, target: str, timestamp_ms: str,
                           nonce: str, body: str = "") -> str:
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    return "\n".join([str(chain_id), method.upper(), target, timestamp_ms, nonce, body_hash])


def sign_rest_request(priv, chain_id: int, method: str, target: str, body: str = ""):
    """Returns (signature, timestamp_ms, nonce) — all strings, ready for the X-API-* headers."""
    timestamp = str(int(time.time() * 1000))
    nonce = b64url(secrets.token_bytes(16))
    canonical = rest_canonical_string(chain_id, method, target, timestamp, nonce, body)
    signature = b64url(priv.sign(canonical.encode()))
    return signature, timestamp, nonce


def ws_signin_canonical_string(chain_id: int, timestamp_ms: str, nonce: str) -> str:
    return "\n".join([str(chain_id), "trading-ws-signin", timestamp_ms, nonce])


def sign_ws_signin(priv, chain_id: int):
    timestamp = str(int(time.time() * 1000))
    nonce = b64url(secrets.token_bytes(16))
    canonical = ws_signin_canonical_string(chain_id, timestamp, nonce)
    signature = b64url(priv.sign(canonical.encode()))
    return signature, timestamp, nonce


def sign_enrollment_payload(wallet_account, ed25519_priv, typed_data: dict):
    """
    One-time key enrollment signing — needs the MAIN WALLET's key (wallet_account,
    an eth_account LocalAccount), same "only touched once, never held by the bot"
    pattern as RiseX's registration. Perpl's own docs say most users don't need
    this at all (create a key at testnet.perpl.xyz/apikeys / app.perpl.xyz/apikeys
    instead) — this exists for completeness, but programmatic enrollment also
    needs a Perpl-whitelisted Origin, an external dependency this can't satisfy
    on its own.

    typed_data: the exact object returned by POST /v1/api-key/payload, unmodified.
    Returns (wallet_signature_hex, pop_signature_hex) to submit to /v1/api-key/enroll.
    """
    from eth_account.messages import encode_typed_data

    wallet_sig = wallet_account.sign_message(encode_typed_data(full_message=typed_data))
    wallet_signature_hex = "0x" + wallet_sig.signature.hex().removeprefix("0x")

    # docs: PoP is an Ed25519 signature over keccak256(0x1901 || domainSeparator ||
    # hashStruct(message)) — exactly what eth_account computes as message_hash.
    pop_sig = ed25519_priv.sign(bytes(wallet_sig.message_hash))
    pop_signature_hex = "0x" + pop_sig.hex()

    return wallet_signature_hex, pop_signature_hex
