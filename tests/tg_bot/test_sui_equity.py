import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "tg_bot"))

import pytest

import sui_equity


class _Resp:
    def __init__(self, status, json_data=None, text=""):
        self.status_code = status
        self._json = json_data
        self.text = text

    def json(self):
        return self._json


scenarios = []


def _fake_post(records):
    calls = {"n": 0}

    def _post(url, json=None, timeout=None):
        calls["n"] += 1
        return records[url]

    return _post, calls


class _FakeGet:
    def __init__(self, records):
        self._records = records

    def __call__(self, url, timeout=None):
        return self._records[url]


def test_sui_equity_combines_wallet_and_aftermath(monkeypatch):
    bot = {"wallet_addr": "0xabc", "network": "mainnet", "chain": "sui"}

    # Wallet GraphQL balance -> USDC 500.0, SUI 2.0
    gql_hit = {"n": 0}

    def fake_post(url, json=None, timeout=None, **kw):
        if "graphql" in url:
            gql_hit["n"] += 1
            coin = (json or {}).get("query", "")
            if "0x2::sui::SUI" in coin:
                return _Resp(200, {"data": {"address": {"balance": {"totalBalance": "2000000000"}}}})
            return _Resp(200, {"data": {"address": {"balance": {"totalBalance": "500000000"}}}})
        if url.endswith("/perpetuals/accounts/owned"):
            # collateral 100 USDC
            return _Resp(200, {"data": {"accountCaps": [{"collateral": "100000000n"}]}})
        if url.endswith("/ccxt/accounts"):
            return _Resp(200, [{"type": "account", "accountNumber": 508}])
        if url.endswith("/ccxt/positions"):
            return _Resp(200, [{"symbol": "SUI/USD:USDC", "unrealizedPnl": "25.5"}])
        if url.endswith("/ccxt/orderbook"):
            return _Resp(200, {"bids": [["0.80", "10"]], "asks": [["0.82", "10"]]})
        return _Resp(200, {})

    monkeypatch.setattr(sui_equity.requests, "post", fake_post)

    def fake_get(url, timeout=None, **kw):
        if url.endswith("/ccxt/markets"):
            return _Resp(200, [{"base": "SUI", "id": "777", "swap": True}])
        if url.endswith("/ccxt/orderbook"):
            return _Resp(200, {"bids": [["0.80", "10"]], "asks": [["0.82", "10"]]})
        return _Resp(200, {})

    monkeypatch.setattr(sui_equity.requests, "get", fake_get)

    snap = sui_equity.sui_equity(bot)
    assert snap["usdc"] == 500.0
    assert snap["sui"] == 2.0
    assert snap["collateral"] == 100.0
    assert abs(snap["unrealized_pnl"] - 25.5) < 1e-9
    assert abs(snap["sui_price"] - 0.81) < 1e-9
    assert abs(snap["equity"] - 625.5) < 1e-9
    assert snap["account_number"] == 508


def test_sui_equity_empty_when_no_address(monkeypatch):
    snap = sui_equity.sui_equity({"wallet_addr": "", "network": "mainnet"})
    assert snap["equity"] == 0.0
    assert snap["usdc"] == 0.0
    assert snap["collateral"] == 0.0
    assert snap["unrealized_pnl"] == 0.0
