"""Runtime factory: wires the whole degen package behind one call.

userbot mounts this when DEGEN_ENABLED=1; otherwise the feature is entirely dark
(imports of the package stay cheap). The runtime owns: chain client, ledger,
executor, event bus, streamer, order evaluator, sniper — and exposes a
`DegenUI(...)` bound to the per-bot context.

Keys NEVER enter this module: `adapter_for(wallet)` is provided by the caller
(userbot) which decrypts via ExecVault at signing time and returns a bound
SUIAdapter. That keeps one trust boundary (docs §3.5/§3.6).
"""

from __future__ import annotations

import logging
import os
import threading

from .chain import Chain
from .db import DegenLedger
from .executor import DegenExecutor
from .orders import OrderEvaluator
from .sniper import Sniper
from .streamer import EventBus, SuipumpStreamer
from .ui import DegenUI

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_SINGLETON: dict = {}


def env_on() -> bool:
    return os.getenv("DEGEN_ENABLED", "0") == "1"


def ledger_path() -> str:
    return os.getenv("DEGEN_LEDGER_PATH", "degen_ledger.db")


def get_ledger() -> DegenLedger:
    with _LOCK:
        if "led" not in _SINGLETON:
            _SINGLETON["led"] = DegenLedger(ledger_path())
        return _SINGLETON["led"]


class DegenRuntime:
    """One process-wide degen engine; UI instances are per-bot and share it."""

    def __init__(self, network: str | None = None):
        # Degen is ALWAYS mainnet (docs §0 — verified mainnet contracts). The perps
        # EXEC_SUI_NETWORK default (often testnet) must not leak in here.
        self.network = "mainnet"
        self.ch = Chain(self.network)
        self.led = get_ledger()
        self.bus = EventBus()
        self.executor = DegenExecutor(self.ch, self.led,
                                      adapter_for_wallet=lambda w: (None, None),
                                      fee_recipient=os.getenv("NEKO_FEE_ADDR", ""))
        self.evaluator = OrderEvaluator(self.ch, self.led, self.executor)
        self.sniper = Sniper(self.ch, self.led, self.executor)
        self.streamer = SuipumpStreamer(self.ch, self.led, self.bus)
        self._started = False

    def set_wallet_adapter_factory(self, factory) -> None:
        """userbot injects: wallet_addr -> (SUIAdapter|None, key_ref)."""
        self.executor._adapter_for = factory
        self._wallet_factory = factory

    def set_spot_factory(self, factory) -> None:
        self.executor.set_spot_adapter(factory)

    def _wallet_addr(self, bot) -> str:
        """Main wallet address for honeypot sender (§5.3a needs funds to test)."""
        try:
            factory = getattr(self, "_wallet_factory", None)
            if factory:
                _ad, addr = factory("")
                return addr or ""
        except Exception:
            pass
        return ""

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.sniper.attach(self.bus)
        self.bus.on("Graduated", self.evaluator.on_graduated)
        self.streamer.start()
        self.evaluator.start()
        self._start_sweeper()
        log.info("[degen] runtime started (%s)", self.network)

    def _start_sweeper(self) -> None:
        """§6.3c: un-touched cards must vanish at T+180s even with no new commits."""
        import time as _t

        def _loop():
            while True:
                _t.sleep(30)
                try:
                    for row in self.led.due_deletes():
                        # token-based delete needs the bot token; the userbot wires
                        # a deleter, else we just drop the row (harmless).
                        if self._deleter:
                            self._deleter(row)
                        self.led.clear_delete(row["chat_id"], row["message_id"])
                except Exception:
                    log.exception("expiry sweeper")
        self._deleter = None
        threading.Thread(target=_loop, name="degen-sweep", daemon=True).start()

    def set_deleter(self, fn) -> None:
        self._deleter = fn

    def stop(self) -> None:
        self.streamer.stop()
        self.evaluator.stop()

    def ui_for(self, bot_of, ai_key_ok) -> DegenUI:
        from .bundle import BundleManager
        return DegenUI(self.ch, self.led, executor=self.executor,
                       bundles=BundleManager(self.led),
                       bot_of=bot_of, ai_key_ok=ai_key_ok,
                       wallet_addr=self._wallet_addr)


def get_runtime() -> DegenRuntime:
    with _LOCK:
        if "rt" not in _SINGLETON:
            _SINGLETON["rt"] = DegenRuntime()
        return _SINGLETON["rt"]
