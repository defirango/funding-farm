"""
Pure crypto/signing primitives for RISEx: EIP-712 permits, the uint88
order-data bit-packing, action-hash builders, and nonce/bitmap tracking.

No network calls in this file — see risex_client.py for the REST wrapper.
Spec source: https://developer.rise.trade/reference/integration (fetched
and cross-checked field-by-field against the worked Python example there).
"""

import time

from eth_utils import keccak

MAX_NONCE_BITMAP_INDEX = 207  # docs: "maxes out at 207, not 255" — 208+ reverts

EIP712_DOMAIN_TYPES = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]


# ── uint88 order-data packing ───────────────────────────────────────────
#
# [87:70] marketId (16b) [69:38] sizeSteps (32b) [37:14] priceTicks (24b)
# [13:6] orderFlags (8b) [5:1] version=1 (5b) [0] reserved
#
# orderFlags: bit0=side(1=Sell) bit1=postOnly bit2=reduceOnly bits4:3=stpMode
#             bit5=orderType(1=Limit) bits7:6=timeInForce

def _check_range(name: str, value: int, bits: int) -> None:
    limit = 1 << bits
    if not (0 <= value < limit):
        raise ValueError(f"{name}={value} out of range for a {bits}-bit field (0..{limit - 1})")


def pack_order_uint88(*, market_id: int, size_steps: int, price_ticks: int, side: int,
                       post_only: bool, reduce_only: bool, stp_mode: int,
                       order_type: int, time_in_force: int) -> int:
    _check_range("market_id", market_id, 16)
    _check_range("size_steps", size_steps, 32)
    _check_range("price_ticks", price_ticks, 24)
    _check_range("stp_mode", stp_mode, 2)
    _check_range("time_in_force", time_in_force, 2)
    if side not in (0, 1):
        raise ValueError(f"side must be 0 (Buy) or 1 (Sell), got {side}")
    if order_type not in (0, 1):
        raise ValueError(f"order_type must be 0 (Market) or 1 (Limit), got {order_type}")

    flags = (
        (side & 1)
        | ((1 if post_only else 0) << 1)
        | ((1 if reduce_only else 0) << 2)
        | ((stp_mode & 0b11) << 3)
        | ((order_type & 1) << 5)
        | ((time_in_force & 0b11) << 6)
    )
    version = 1
    return (
        (market_id << 70)
        | (size_steps << 38)
        | (price_ticks << 14)
        | (flags << 6)
        | (version << 1)
    )


def unpack_order_uint88(order_data: int) -> dict:
    flags = (order_data >> 6) & 0xFF
    return {
        "market_id": (order_data >> 70) & 0xFFFF,
        "size_steps": (order_data >> 38) & 0xFFFFFFFF,
        "price_ticks": (order_data >> 14) & 0xFFFFFF,
        "side": flags & 1,
        "post_only": bool((flags >> 1) & 1),
        "reduce_only": bool((flags >> 2) & 1),
        "stp_mode": (flags >> 3) & 0b11,
        "order_type": (flags >> 5) & 1,
        "time_in_force": (flags >> 6) & 0b11,
        "version": (order_data >> 1) & 0b11111,
        "reserved": order_data & 0b1,
    }


# ── action hashes ───────────────────────────────────────────────────────

def _word(value: int) -> bytes:
    return int(value).to_bytes(32, "big")


def place_order_action_hash(order_data: int, *, builder_id: int = 0,
                             builder_fee_bps: int = 0, client_order_id: int = 0,
                             ttl_units: int = 0) -> bytes:
    header_flags = 0x01  # permit always present
    if builder_id:
        header_flags |= 0x02
    if client_order_id:
        header_flags |= 0x04
    if ttl_units:
        header_flags |= 0x10

    words = [keccak(b"RISE_PERPS_PLACE_ORDER_V1"), _word(header_flags), _word(order_data), _word(builder_id)]
    if builder_fee_bps > 0:  # word is omitted entirely when there's no builder fee
        words.append(_word(builder_fee_bps))
    words.append(_word(client_order_id))
    words.append(_word(ttl_units))
    return keccak(b"".join(words))


def cancel_order_action_hash(market_id: int, resting_order_id: int) -> bytes:
    return keccak(b"".join([
        keccak(b"RISE_PERPS_CANCEL_ORDER_V1"),
        _word(market_id),
        _word(resting_order_id),
    ]))


def cancel_all_orders_action_hash(*_args, **_kwargs) -> bytes:
    # The docs name the selector (RISE_PERPS_CANCEL_ALL_ORDERS_V1) but never
    # publish a worked hash-preimage example the way place/cancel-single do.
    # Guessing the field layout for something that cancels live orders is
    # exactly the kind of mistake that's cheap to make and expensive to be
    # wrong about — confirm the real preimage against a testnet response
    # (or updated docs) before implementing this for real.
    raise NotImplementedError(
        "cancel-all action hash is not documented with a worked example — "
        "verify RISE_PERPS_CANCEL_ALL_ORDERS_V1's preimage against testnet "
        "before implementing. Cancel orders individually until then."
    )


# ── nonce / bitmap tracking ─────────────────────────────────────────────

class NonceTracker:
    """
    One tracker per session. Docs: sign with (server's current anchor + 1),
    starting at bit 0, to avoid colliding with any in-flight permit under
    the account's last-used anchor. Bits 0..207 are usable per anchor;
    208 reverts, so roll to a fresh anchor instead.
    """

    def __init__(self, server_current_anchor: int):
        self.anchor = server_current_anchor + 1
        self._next_bit = 0

    def next(self) -> tuple:
        if self._next_bit > MAX_NONCE_BITMAP_INDEX:
            self.anchor += 1
            self._next_bit = 0
        anchor, bit = self.anchor, self._next_bit
        self._next_bit += 1
        return anchor, bit


# ── EIP-712 signing ─────────────────────────────────────────────────────

def _sign_typed(account, domain: dict, types: dict, primary_type: str, message: dict):
    from eth_account.messages import encode_typed_data
    full_message = {
        "types": {"EIP712Domain": EIP712_DOMAIN_TYPES, **types},
        "primaryType": primary_type,
        "domain": domain,
        "message": message,
    }
    return account.sign_message(encode_typed_data(full_message=full_message))


def _to_compact_signature(sig) -> bytes:
    """64-byte EIP-2098 compact sig: r || yParityAndS (top bit of s set when v==28)."""
    r = sig.r.to_bytes(32, "big")
    s = bytearray(sig.s.to_bytes(32, "big"))
    if sig.v == 28:
        s[0] |= 0x80
    return r + bytes(s)


def build_permit(signer_account, *, account_address: str, domain: dict, router_address: str,
                  action_hash: bytes, nonce_anchor: int, nonce_bitmap_index: int,
                  ttl_seconds: int = 3600) -> dict:
    import base64

    deadline = int(time.time()) + ttl_seconds
    types = {"VerifyWitness": [
        {"name": "account", "type": "address"},
        {"name": "target", "type": "address"},
        {"name": "hash", "type": "bytes32"},
        {"name": "nonceAnchor", "type": "uint48"},
        {"name": "nonceBitmap", "type": "uint8"},
        {"name": "deadline", "type": "uint32"},
    ]}
    message = {
        "account": account_address,
        "target": router_address,
        "hash": action_hash,
        "nonceAnchor": nonce_anchor,
        "nonceBitmap": nonce_bitmap_index,
        "deadline": deadline,
    }
    sig = _sign_typed(signer_account, domain, types, "VerifyWitness", message)
    return {
        "account": account_address,
        "signer": signer_account.address,
        "nonce_anchor": str(nonce_anchor),
        "nonce_bitmap_index": nonce_bitmap_index,
        "deadline": deadline,
        "signature": base64.b64encode(_to_compact_signature(sig)).decode(),
    }


def build_register_signer_signatures(account, signer, *, message_text: str, expiration: int,
                                      nonce_anchor: int, nonce_bitmap_index: int, domain: dict):
    reg_types = {"RegisterSigner": [
        {"name": "account", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "message", "type": "string"},
        {"name": "expiration", "type": "uint32"},
        {"name": "nonceAnchor", "type": "uint48"},
        {"name": "nonceBitmap", "type": "uint8"},
    ]}
    reg_message = {
        "account": account.address, "signer": signer.address, "message": message_text,
        "expiration": expiration, "nonceAnchor": nonce_anchor, "nonceBitmap": nonce_bitmap_index,
    }
    account_sig = _sign_typed(account, domain, reg_types, "RegisterSigner", reg_message)

    ver_types = {"VerifySigner": [
        {"name": "account", "type": "address"},
        {"name": "nonceAnchor", "type": "uint48"},
        {"name": "nonceBitmap", "type": "uint8"},
    ]}
    ver_message = {"account": account.address, "nonceAnchor": nonce_anchor, "nonceBitmap": nonce_bitmap_index}
    signer_sig = _sign_typed(signer, domain, ver_types, "VerifySigner", ver_message)

    return (
        "0x" + account_sig.signature.hex().removeprefix("0x"),
        "0x" + signer_sig.signature.hex().removeprefix("0x"),
    )


def build_revoke_signer_signature(account, signer_address: str, *, nonce_anchor: int,
                                   nonce_bitmap_index: int, domain: dict) -> str:
    types = {"RevokeSigner": [
        {"name": "account", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "nonceAnchor", "type": "uint48"},
        {"name": "nonceBitmap", "type": "uint8"},
    ]}
    message = {
        "account": account.address, "signer": signer_address,
        "nonceAnchor": nonce_anchor, "nonceBitmap": nonce_bitmap_index,
    }
    sig = _sign_typed(account, domain, types, "RevokeSigner", message)
    return "0x" + sig.signature.hex().removeprefix("0x")
