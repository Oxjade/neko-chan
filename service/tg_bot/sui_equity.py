"""Real Sui equity snapshot for the Aftermath copy-trading wallet.

Equity = wallet USDC + wallet SUI x live price + Aftermath perp collateral
        + unrealized perp PnL.

All reads are public / address-based (no private key required): Sui GraphQL for
wallet balances, and the Aftermath public REST endpoints for the perp account
(collateral + open position unrealized PnL). Designed for the Telegram watcher
and dashboard so reported Equities/P&L always reflect the real funded account
instead of the old hard-coded $100k paper baseline.
"""

import logging

import requests

LOG = logging.getLogger("tg_bot.sui_equity")

AFTERMATH_API = "https://aftermath.finance/api"
AFTERMATH_TESTNET_API = "https://testnet.aftermath.finance/api"

SUI_USDC_MAINNET = "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC"
SUI_USDC_TESTNET = "0xcdd397f2cffb7f5d439f56fc01afe5585c5f06e3bcd2ee3a21753c566de313d9::usdc::USDC"


def _wallet_balances(bot: dict) -> tuple[float, float]:
    """(USDC, SUI) on-chain for the bot wallet via Sui GraphQL."""
    addr = str(bot.get("wallet_addr") or "").strip()
    if not addr:
        return 0.0, 0.0
    network = (bot.get("network") or "mainnet").strip().lower()
    testnet = network != "mainnet"
    gql = f"https://graphql.{'testnet' if testnet else 'mainnet'}.sui.io/graphql"
    usdc_type = SUI_USDC_TESTNET if testnet else SUI_USDC_MAINNET
    out = {"USDC": 0.0, "SUI": 0.0}
    for key, coin, dec in (("SUI", "0x2::sui::SUI", 9), ("USDC", usdc_type, 6)):
        try:
            q = ('{ address(address: "' + addr + '") { balance(coinType: "' + coin + '") '
                 '{ totalBalance } } }')
            r = requests.post(gql, json={"query": q}, timeout=8)
            data = r.json()
            out[key] = float(int((data.get("data") or {})
                                 .get("address", {}).get("balance", {}).get("totalBalance", 0) or 0)) / (10 ** dec)
        except Exception as exc:
            LOG.warning("[eq] balance %s failed: %s", key, exc)
    return out["USDC"], out["SUI"]


def _aftermath(bot: dict) -> tuple[float, float, int | None]:
    """(collateral_usdc, unrealized_pnl_usdc, account_number) via public API."""
    addr = str(bot.get("wallet_addr") or "").strip()
    if not addr:
        return 0.0, 0.0, None
    network = (bot.get("network") or "mainnet").strip().lower()
    api = AFTERMATH_TESTNET_API if network != "mainnet" else AFTERMATH_API
    collateral = 0.0
    try:
        r = requests.post(f"{api}/perpetuals/accounts/owned",
                          json={"walletAddress": addr}, timeout=15)
        if r.status_code == 200:
            caps = (r.json().get("data") or {}).get("accountCaps") or []
            for c in caps:
                try:
                    collateral = float(str(c.get("collateral") or "0n").rstrip("n")) / 1e6
                    break
                except Exception:
                    continue
    except Exception as exc:
        LOG.warning("[eq] aftermath collateral failed: %s", exc)

    acc_num = None
    try:
        r = requests.post(f"{api}/ccxt/accounts", json={"address": addr}, timeout=15)
        if r.status_code == 200:
            for a in (r.json() or []):
                if isinstance(a, dict) and a.get("type") == "account" and a.get("accountNumber") is not None:
                    acc_num = int(a["accountNumber"])
                    break
    except Exception as exc:
        LOG.warning("[eq] aftermath account lookup failed: %s", exc)

    unreal = 0.0
    if acc_num is not None:
        try:
            r = requests.post(f"{api}/ccxt/positions", json={"accountNumber": acc_num}, timeout=15)
            if r.status_code == 200:
                rows = r.json() or []
                for p in rows if isinstance(rows, list) else []:
                    if isinstance(p, dict):
                        try:
                            unreal += float(p.get("unrealizedPnl") or 0.0)
                        except Exception:
                            pass
        except Exception as exc:
            LOG.warning("[eq] aftermath positions failed: %s", exc)

    return collateral, unreal, acc_num


def _sui_price_usd() -> float:
    """Live SUI price from Aftermath's SUI/USD:USDC perp orderbook (mid)."""
    try:
        r = requests.get(f"{AFTERMATH_API}/ccxt/markets", timeout=10)
        if r.status_code != 200:
            return 0.0
        ch_id = None
        for m in (r.json() or []):
            if isinstance(m, dict) and str(m.get("base") or "").upper() == "SUI" and m.get("swap"):
                ch_id = m.get("id")
                break
        if not ch_id:
            return 0.0
        r = requests.post(f"{AFTERMATH_API}/ccxt/orderbook", json={"chId": ch_id}, timeout=10)
        if r.status_code != 200:
            return 0.0
        data = r.json()
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        bb = max((float(x[0]) for x in bids if x and len(x) > 1), default=None)
        ba = min((float(x[0]) for x in asks if x and len(x) > 1), default=None)
        if bb is not None and ba is not None:
            return (bb + ba) / 2.0
        return bb or ba or 0.0
    except Exception as exc:
        LOG.warning("[eq] SUI price failed: %s", exc)
        return 0.0


def sui_equity(bot: dict) -> dict:
    """Full real-equity snapshot for a bot (Sui/Aftermath).

    Returns keys: usdc, sui, sui_price, sui_value, collateral, unrealized_pnl,
    equity (== usdc + collateral + unrealized_pnl; SUI not counted toward trade
    equity since it is only gas, but reported separately with its USD value).
    """
    usdc, sui = _wallet_balances(bot)
    collateral, unreal, acc_num = _aftermath(bot)
    price = _sui_price_usd()
    equity = usdc + collateral + unreal
    return {
        "usdc": usdc,
        "sui": sui,
        "sui_price": price,
        "sui_value": sui * price,
        "collateral": collateral,
        "unrealized_pnl": unreal,
        "account_number": acc_num,
        "equity": equity,
    }
