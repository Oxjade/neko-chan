"""Wallet UI renderers: per-chain wallet state for the Telegram bot.
Pure functions - never leak key material (only masked addresses)."""

from exec_vault import ExecVault

CHAIN_LABELS = {"hyperliquid": "🔗 Hyperliquid", "solana": "🔗 Solana", "sui": "🔗 Sui"}

# Release plan: Sui (Bluefin perps) is live now; every other chain carries the
# "code is live, Neko is working hard to make it better for you" status until
# it is fully validated and released slowly.
LIVE_CHAINS = {"sui"}
COMING_SOON_MSG = "code is live, Neko is working hard to make it better for you 🐾"


def render_wallet_panel(bots: list[dict], wallets: dict, chain_state: dict) -> str:
    """bots: [{id, bot_name, paused}], wallets: {chain: wallet-row},
    chain_state: {chain: {balances, positions}}. Pure text (HTML)."""
    line = "─" * 28
    parts = ["<b>💼 Wallet — Neko Real Trading</b>", f"<code>{line}</code>"]
    for chain, label in CHAIN_LABELS.items():
        w = wallets.get(chain)
        if chain not in LIVE_CHAINS:
            parts.append(f"{label} · 🟢 code is live")
            parts.append(f"    {COMING_SOON_MSG}")
            continue
        if not w:
            parts.append(f"{label} · ⚪ NOT CONNECTED")
            parts.append("[connect via Bot Settings]")
            continue
        addr = w["address"]
        masked = f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr
        status = w.get("status", "created")
        status_icon = {"active": "🟢 ACTIVE", "funded": "🟡 FUNDED",
                       "created": "🟠 CREATED", "revoked": "🔴 REVOKED"}.get(status, status)
        bal = chain_state.get(chain, {}).get("balances", {})
        usdc = float(bal.get("USDC", 0))
        native = float(bal.get("native", 0))
        npos = len(chain_state.get(chain, {}).get("positions", []))
        parts.append(
            f"{label} · <code>{masked}</code> · {status_icon}\n"
            f"    USDC <code>${usdc:,.2f}</code> · native {native:,.4f} · {npos} open"
        )
    parts.append(f"<code>{line}</code>")
    parts.append("💱 Forex — coming soon 🔜")
    return "\n".join(parts)


def render_killswitch_line(engaged: bool) -> str:
    if engaged:
        return "🛑 KILL-SWITCH ENGAGED — all trading halted"
    return "🛑 Kill-switch armed · available at any time"