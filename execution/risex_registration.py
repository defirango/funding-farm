"""
One-time signer registration/revocation. These need the MAIN account's
private key — the thing the bot's regular runtime (risex_client.py) should
never hold. Run this interactively: on testnet with a throwaway generated
wallet (see risex_testnet_selftest.py), or on mainnet with your own wallet,
run by you — never wired into an unattended workflow.
"""

import time

import requests

from execution.risex_signing import build_register_signer_signatures, build_revoke_signer_signature


def _unwrap(payload):
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def get_domain(base_url: str) -> dict:
    from eth_utils import to_checksum_address
    resp = requests.get(base_url + "/v1/auth/eip712-domain", timeout=15)
    resp.raise_for_status()
    d = _unwrap(resp.json())
    return {
        "name": d["name"], "version": d["version"], "chainId": int(d["chain_id"]),
        "verifyingContract": to_checksum_address(d["verifying_contract"]),
    }


def get_nonce_anchor(base_url: str, account_address: str) -> int:
    resp = requests.get(f"{base_url}/v1/nonce-state/{account_address}", timeout=15)
    resp.raise_for_status()
    return int(_unwrap(resp.json())["nonce_anchor"])


def register_signer(base_url: str, account, signer, *, message_text: str = "RISEx session key",
                     ttl_days: int = 30) -> dict:
    domain = get_domain(base_url)
    anchor = get_nonce_anchor(base_url, account.address) + 1
    expiration = int(time.time()) + ttl_days * 86400

    account_sig, signer_sig = build_register_signer_signatures(
        account, signer, message_text=message_text, expiration=expiration,
        nonce_anchor=anchor, nonce_bitmap_index=0, domain=domain,
    )
    body = {
        "account": account.address, "signer": signer.address, "message": message_text,
        "nonce_anchor": str(anchor), "nonce_bitmap_index": 0, "expiration": str(expiration),
        "account_signature": account_sig, "signer_signature": signer_sig,
    }
    resp = requests.post(base_url + "/v1/auth/register-signer", json=body, timeout=15)
    resp.raise_for_status()
    return _unwrap(resp.json())


def revoke_signer(base_url: str, account, signer_address: str) -> dict:
    # NOT WORKING as of this writing — confirmed live against testnet
    # 2026-09-15. The docs give the RevokeSigner EIP-712 type but no worked
    # REST body example (unlike register-signer), so this body shape is
    # inferred by analogy (account-only signature, no signer_signature).
    # Submitting it gets past request validation (the server accepts the
    # signature and relays a real on-chain tx — ruled out by testing: a
    # deliberately-reused nonce gets a specific "NonceUsed" revert, while
    # this gets a generic "RevokeSigner transaction reverted" with a
    # tx_hash but no reason). Cause not yet identified — could be a wrong
    # field, a missing co-signature the docs don't mention, or a contract-
    # side precondition (cooldown, signer-usage requirement, etc.). Needs
    # RiseX support/docs or a working reference implementation before this
    # can be trusted — don't rely on it to actually revoke a compromised
    # key until it's proven live.
    domain = get_domain(base_url)
    anchor = get_nonce_anchor(base_url, account.address) + 1
    signature = build_revoke_signer_signature(
        account, signer_address, nonce_anchor=anchor, nonce_bitmap_index=0, domain=domain,
    )
    body = {
        "account": account.address, "signer": signer_address,
        "nonce_anchor": str(anchor), "nonce_bitmap_index": 0, "signature": signature,
    }
    resp = requests.post(base_url + "/v1/auth/revoke-signer", json=body, timeout=15)
    resp.raise_for_status()
    return _unwrap(resp.json())
