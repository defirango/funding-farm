"""
Pure-function tests for risex_signing.py — no network calls. Each expected
value is computed independently in the test (not by re-calling the function
under test), mirroring the exact abi.encode/bit-packing formulas from
developer.rise.trade/reference/integration, so a bug in the implementation
can't also be baked into the "expected" value.
"""

import unittest

from eth_utils import keccak

from execution.risex_signing import (
    MAX_NONCE_BITMAP_INDEX,
    NonceTracker,
    cancel_all_orders_action_hash,
    cancel_order_action_hash,
    pack_order_uint88,
    place_order_action_hash,
    unpack_order_uint88,
)


def _word(v: int) -> bytes:
    return int(v).to_bytes(32, "big")


class PackOrderUint88Test(unittest.TestCase):
    def test_matches_doc_worked_example(self):
        # docs' inline example: postOnly + Limit + GTC, Buy, market_id=1
        size_steps, price_ticks = 200, 550248
        flags = (0 | (1 << 1) | (1 << 5) | (0 << 6))
        expected = (1 << 70) | (size_steps << 38) | (price_ticks << 14) | (flags << 6) | (1 << 1)

        actual = pack_order_uint88(
            market_id=1, size_steps=size_steps, price_ticks=price_ticks, side=0,
            post_only=True, reduce_only=False, stp_mode=0, order_type=1, time_in_force=0,
        )
        self.assertEqual(actual, expected)

    def test_round_trip_various_values(self):
        cases = [
            dict(market_id=1, size_steps=200, price_ticks=550248, side=0,
                 post_only=True, reduce_only=False, stp_mode=0, order_type=1, time_in_force=0),
            dict(market_id=65535, size_steps=2**32 - 1, price_ticks=2**24 - 1, side=1,
                 post_only=False, reduce_only=True, stp_mode=2, order_type=0, time_in_force=3),
            dict(market_id=0, size_steps=0, price_ticks=0, side=0,
                 post_only=False, reduce_only=False, stp_mode=0, order_type=0, time_in_force=0),
        ]
        for kwargs in cases:
            packed = pack_order_uint88(**kwargs)
            unpacked = unpack_order_uint88(packed)
            for key in ("market_id", "size_steps", "price_ticks", "side",
                        "post_only", "reduce_only", "stp_mode", "order_type", "time_in_force"):
                self.assertEqual(unpacked[key], kwargs[key], f"{key} mismatch for {kwargs}")
            self.assertEqual(unpacked["version"], 1)
            self.assertEqual(unpacked["reserved"], 0)

    def test_fits_in_88_bits(self):
        packed = pack_order_uint88(
            market_id=65535, size_steps=2**32 - 1, price_ticks=2**24 - 1, side=1,
            post_only=True, reduce_only=True, stp_mode=2, order_type=1, time_in_force=3,
        )
        self.assertLess(packed, 1 << 88)
        self.assertGreaterEqual(packed, 0)

    def test_rejects_out_of_range_fields(self):
        base = dict(market_id=1, size_steps=1, price_ticks=1, side=0, post_only=False,
                    reduce_only=False, stp_mode=0, order_type=1, time_in_force=0)
        for field, bad_value in (("market_id", 1 << 16), ("size_steps", 1 << 32),
                                  ("price_ticks", 1 << 24), ("stp_mode", 4), ("time_in_force", 4)):
            with self.assertRaises(ValueError):
                pack_order_uint88(**{**base, field: bad_value})
        with self.assertRaises(ValueError):
            pack_order_uint88(**{**base, "side": 2})
        with self.assertRaises(ValueError):
            pack_order_uint88(**{**base, "order_type": 2})


class ActionHashTest(unittest.TestCase):
    def test_place_order_hash_matches_doc_worked_example(self):
        order_data = 12345
        expected = keccak(
            keccak(b"RISE_PERPS_PLACE_ORDER_V1") + _word(0x01) + _word(order_data) + _word(0) + _word(0) + _word(0)
        )
        actual = place_order_action_hash(order_data)
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 32)

    def test_place_order_hash_includes_builder_fee_word_only_when_nonzero(self):
        order_data = 999
        # builder_fee_bps == 0 -> 6 words total (fee word omitted)
        no_fee = keccak(
            keccak(b"RISE_PERPS_PLACE_ORDER_V1") + _word(0x03) + _word(order_data) + _word(7) + _word(0) + _word(0)
        )
        self.assertEqual(place_order_action_hash(order_data, builder_id=7), no_fee)

        # builder_fee_bps > 0 -> 7 words total (fee word present)
        with_fee = keccak(
            keccak(b"RISE_PERPS_PLACE_ORDER_V1") + _word(0x03) + _word(order_data)
            + _word(7) + _word(25) + _word(0) + _word(0)
        )
        self.assertEqual(place_order_action_hash(order_data, builder_id=7, builder_fee_bps=25), with_fee)
        self.assertNotEqual(no_fee, with_fee)

    def test_place_order_hash_header_flags_set_for_client_order_id_and_ttl(self):
        order_data = 1
        expected = keccak(
            keccak(b"RISE_PERPS_PLACE_ORDER_V1") + _word(0x15) + _word(order_data) + _word(0) + _word(42) + _word(9)
        )
        actual = place_order_action_hash(order_data, client_order_id=42, ttl_units=9)
        self.assertEqual(actual, expected)

    def test_cancel_order_hash_matches_doc_worked_example(self):
        market_id, resting_order_id = 1, 987654321
        expected = keccak(
            keccak(b"RISE_PERPS_CANCEL_ORDER_V1") + _word(market_id) + _word(resting_order_id)
        )
        self.assertEqual(cancel_order_action_hash(market_id, resting_order_id), expected)

    def test_cancel_all_is_deliberately_unimplemented(self):
        with self.assertRaises(NotImplementedError):
            cancel_all_orders_action_hash()


class NonceTrackerTest(unittest.TestCase):
    def test_starts_at_server_anchor_plus_one_bit_zero(self):
        tracker = NonceTracker(server_current_anchor=5)
        anchor, bit = tracker.next()
        self.assertEqual(anchor, 6)
        self.assertEqual(bit, 0)

    def test_bits_increment_sequentially_within_anchor(self):
        tracker = NonceTracker(server_current_anchor=0)
        seen = [tracker.next() for _ in range(5)]
        self.assertEqual(seen, [(1, 0), (1, 1), (1, 2), (1, 3), (1, 4)])

    def test_rolls_over_to_new_anchor_after_max_bitmap_index(self):
        tracker = NonceTracker(server_current_anchor=0)
        results = [tracker.next() for _ in range(MAX_NONCE_BITMAP_INDEX + 1)]  # bits 0..207, all anchor 1
        self.assertTrue(all(a == 1 for a, _ in results))
        self.assertEqual([b for _, b in results], list(range(MAX_NONCE_BITMAP_INDEX + 1)))

        next_anchor, next_bit = tracker.next()  # the 209th call must roll over
        self.assertEqual(next_anchor, 2)
        self.assertEqual(next_bit, 0)


if __name__ == "__main__":
    unittest.main()
