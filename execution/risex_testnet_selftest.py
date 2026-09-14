#!/usr/bin/env python3
"""
End-to-end proof that risex_client.py / risex_signing.py / risex_registration.py
work against a live server: generate throwaway keys, faucet-fund them, register
a signer, place a tiny post-only order far from mark, confirm it rests, cancel
it, confirm it's gone, revoke the signer.

Testnet only (https://api.testnet.rise.trade) — play money from RiseX's own
faucet. Never touches mainnet or any real key; the throwaway keys live only
in this process's memory.

Run:  .venv/bin/python3 -m execution.risex_testnet_selftest
"""

import sys
import time

from eth_account import Account

from execution.risex_client import TESTNET_BASE_URL, RiseXClient
from execution.risex_registration import register_signer, revoke_signer

RESULTS = []


def record(label, ok, detail=""):
    RESULTS.append((label, ok))
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {label}" + (f" — {detail}" if detail else ""))


def finish():
    print("\n" + "=" * 50)
    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"{passed}/{len(RESULTS)} steps passed")
    ok = len(RESULTS) > 0 and all(ok for _, ok in RESULTS)
    print("OVERALL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    print(f"RiseX testnet selftest — {TESTNET_BASE_URL}\n")

    account = Account.create()
    signer = Account.create()
    print(f"throwaway account: {account.address}")
    print(f"throwaway signer:  {signer.address}\n")

    client = RiseXClient(base_url=TESTNET_BASE_URL, account_address=account.address, signer=signer)

    try:
        client.faucet_deposit("1000")
        record("faucet deposit", True)
    except Exception as e:
        record("faucet deposit", False, str(e))
        return finish()

    time.sleep(5)  # let the chain event feed index the deposit before registering

    try:
        register_signer(TESTNET_BASE_URL, account, signer)
        record("register signer", True)
    except Exception as e:
        record("register signer", False, str(e))
        return finish()

    try:
        status = client.session_key_status()
        active = str(status.get("status")) == "1"
        record("session key active", active, f"status={status.get('status')}")
        if not active:
            return finish()
    except Exception as e:
        record("session key active", False, str(e))
        return finish()

    try:
        markets = client.get_markets()
        market = next(m for m in markets if m.get("active") and m["config"].get("unlocked"))
        market_id = int(market["market_id"])
        step_size = float(market["config"]["step_size"])
        step_price = float(market["config"]["step_price"])
        min_order_size = float(market["config"]["min_order_size"])
        mark_price = float(market["mark_price"])

        size = max(min_order_size, step_size * 200)
        size_steps = int(round(size / step_size))
        buy_price = mark_price * 0.85  # 15% below mark — post-only buy won't fill
        price_ticks = int(buy_price / step_price)

        print(f"market: {market['display_name']} (id={market_id}), "
              f"mark={mark_price}, order price={buy_price:.4f}, size_steps={size_steps}")
        record("market + order params", True)
    except Exception as e:
        record("market + order params", False, str(e))
        return finish()

    try:
        placed = client.place_order(
            market_id=market_id, size_steps=size_steps, price_ticks=price_ticks, side=0,
            post_only=True, reduce_only=False, stp_mode=0, order_type=1, time_in_force=0,
        )
        order_id = placed["order_id"]
        record("place order", True, f"order_id={order_id}")
    except Exception as e:
        record("place order", False, str(e))
        return finish()

    try:
        time.sleep(2)
        open_orders = client.get_open_orders(market_id=market_id)
        match = next((o for o in open_orders if o["order_id"] == order_id), None)
        record("order visible in open orders", match is not None)
        if match is None:
            return finish()
        resting_order_id = int(match["resting_order_id"])
    except Exception as e:
        record("order visible in open orders", False, str(e))
        return finish()

    try:
        client.cancel_order(market_id=market_id, order_id=order_id, resting_order_id=resting_order_id)
        record("cancel order", True)
    except Exception as e:
        record("cancel order", False, str(e))
        return finish()

    try:
        time.sleep(2)
        open_orders = client.get_open_orders(market_id=market_id)
        still_open = any(o["order_id"] == order_id for o in open_orders)
        record("order gone after cancel", not still_open)
    except Exception as e:
        record("order gone after cancel", False, str(e))

    try:
        revoke_signer(TESTNET_BASE_URL, account, signer.address)
        record("revoke signer", True)
    except Exception as e:
        record("revoke signer", False, str(e))

    return finish()


if __name__ == "__main__":
    sys.exit(main())
