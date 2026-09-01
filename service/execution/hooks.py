"""Integration: register the three chain adapters' flat_and_cancel hooks."""

from killswitch import KillSwitch


def register_chain_hooks(ks: KillSwitch, adapters: dict) -> None:
    """adapters: {chain: adapter}. Registers flat_and_cancel for each."""
    for chain, adapter in adapters.items():
        ks.register_hook(chain, adapter.flat_and_cancel)


def build_adapters(ledger, vault, cfg: dict) -> dict:
    """Instantiate adapters from cfg {'chain': {'key_enc': bytes, 'master_address': str,
    'rpc_url': str, 'testnet': bool}}. Returns only chains with a configured key."""
    from hl_adapter import HLAdapter
    from sol_adapter import SOLAdapter
    from sui_adapter import SUIAdapter

    adapters = {}
    c = cfg.get("hyperliquid") or {}
    if c.get("key_enc"):
        agent_key = vault.decrypt(c["key_enc"])
        adapters["hyperliquid"] = HLAdapter(ledger, agent_key,
                                            c.get("master_address", ""),
                                            testnet=bool(c.get("testnet")))
    c = cfg.get("solana") or {}
    if c.get("key_enc"):
        key_hex = vault.decrypt(c["key_enc"])
        adapters["solana"] = SOLAdapter(ledger, key_hex,
                                        rpc_url=c.get("rpc_url", ""),
                                        testnet=bool(c.get("testnet")))
    c = cfg.get("sui") or {}
    if c.get("key_enc"):
        from aftermath_adapter import build_aftermath
        key_hex = vault.decrypt(c["key_enc"])
        testnet = bool(c.get("testnet"))
        aftermath_api = c.get("aftermath_api_base", "")
        aftermath = build_aftermath(ledger, key_hex,
                                    api_base=aftermath_api or None,
                                    testnet=testnet,
                                    network=c.get("network", ""))
        adapters["sui"] = SUIAdapter(ledger, key_hex,
                                     rpc_url=c.get("rpc_url", ""),
                                     testnet=testnet,
                                     network=c.get("network", ""),
                                     aftermath=aftermath)
    return adapters