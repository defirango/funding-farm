import unittest

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eth_account import Account

from execution.perpl_signing import (
    rest_canonical_string,
    sign_enrollment_payload,
    sign_rest_request,
    sign_ws_signin,
    ws_signin_canonical_string,
)


class RestCanonicalStringTest(unittest.TestCase):
    def test_matches_doc_field_order_and_body_hash(self):
        # docs: chain_id \n METHOD \n target \n timestamp_ms \n nonce \n sha256(body)_hex
        import hashlib
        body = '{"a":1}'
        expected_hash = hashlib.sha256(body.encode()).hexdigest()
        canonical = rest_canonical_string(143, "post", "/v1/orders", "1700000000000", "abc123", body)
        self.assertEqual(canonical, f"143\nPOST\n/v1/orders\n1700000000000\nabc123\n{expected_hash}")

    def test_empty_body_hashes_empty_string(self):
        import hashlib
        canonical = rest_canonical_string(10143, "GET", "/v1/trading/fills?count=1", "1", "n")
        self.assertTrue(canonical.endswith(hashlib.sha256(b"").hexdigest()))


class SignRestRequestTest(unittest.TestCase):
    def test_signature_verifies_against_exact_canonical_string(self):
        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key()
        signature, timestamp, nonce = sign_rest_request(priv, 10143, "GET", "/v1/trading/fills?count=1")

        canonical = rest_canonical_string(10143, "GET", "/v1/trading/fills?count=1", timestamp, nonce)
        import base64
        raw_sig = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        pub.verify(raw_sig, canonical.encode())  # raises InvalidSignature if wrong

    def test_wrong_canonical_string_fails_verification(self):
        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key()
        signature, timestamp, nonce = sign_rest_request(priv, 10143, "GET", "/v1/trading/fills?count=1")
        import base64
        raw_sig = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        tampered = rest_canonical_string(10143, "GET", "/v1/trading/fills?count=2", timestamp, nonce)
        with self.assertRaises(InvalidSignature):
            pub.verify(raw_sig, tampered.encode())


class WsSigninTest(unittest.TestCase):
    def test_canonical_string_format(self):
        self.assertEqual(
            ws_signin_canonical_string(143, "1700000000000", "abc"),
            "143\ntrading-ws-signin\n1700000000000\nabc",
        )

    def test_signature_verifies(self):
        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key()
        signature, timestamp, nonce = sign_ws_signin(priv, 10143)
        canonical = ws_signin_canonical_string(10143, timestamp, nonce)
        import base64
        raw_sig = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        pub.verify(raw_sig, canonical.encode())


class EnrollmentPayloadSigningTest(unittest.TestCase):
    def test_wallet_signature_recovers_to_correct_address_and_pop_verifies(self):
        wallet = Account.create()
        ed25519_priv = Ed25519PrivateKey.generate()
        ed25519_pub = ed25519_priv.public_key()

        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "ApiKeyAuth": [
                    {"name": "account", "type": "address"},
                    {"name": "publicKey", "type": "bytes32"},
                    {"name": "scopeMask", "type": "uint8"},
                ],
            },
            "primaryType": "ApiKeyAuth",
            "domain": {"name": "Perpl", "version": "1", "chainId": 10143, "verifyingContract": wallet.address},
            "message": {"account": wallet.address, "publicKey": b"\x01" * 32, "scopeMask": 3},
        }

        wallet_sig_hex, pop_sig_hex = sign_enrollment_payload(wallet, ed25519_priv, typed_data)

        from eth_account.messages import encode_typed_data
        message = encode_typed_data(full_message=typed_data)
        recovered_address = Account.recover_message(message, signature=wallet_sig_hex)
        self.assertEqual(recovered_address, wallet.address)

        digest = wallet.sign_message(message).message_hash
        ed25519_pub.verify(bytes.fromhex(pop_sig_hex.removeprefix("0x")), bytes(digest))


if __name__ == "__main__":
    unittest.main()
