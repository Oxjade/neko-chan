"""Aftermath SPOT trading package.

Isolated from the perpetuals execution stack (service/execution/) on purpose:
spot has its own API surface (Router swaps + spot limit orders), its own
adapter, its own risk profile (long-only, no leverage, no funding), and its
own fee flow. Nothing in here imports from service/execution except the
shared order_model schema and the Sui key/vault plumbing.

Layout:
    adapter.py       AftermathSpotAdapter: router swaps + spot limit orders
    order_model.py   SpotOrder / SpotIntent schema (buy/sell, no shorts)
    risk.py          Spot risk guard: balance caps, slippage, min order size
    gateway.py       SpotGateway: spot-specific route/ledger/fee wiring
    README.md        API reference + integration decisions
"""
