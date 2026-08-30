"""Risk guard: hard caps evaluated BEFORE any signing. Never model-overridable."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BotRiskProfile:
    max_notional_usd: float = 500.0          # per order (mainnet v1)
    max_exposure_pct: float = 30.0           # of wallet balance
    max_leverage: float = 100.0              # capped further by venue caps
    require_stop: bool = True                # mandatory stop on leveraged opens
    min_stop_pct: float = 2.0
    max_stop_pct: float = 8.0
    daily_loss_halt_pct: float = 3.0         # flat + halt below this
    max_open_positions: int = 5


@dataclass
class WalletState:
    usd_balance: float = 0.0
    open_exposure_usd: float = 0.0
    open_positions: int = 0
    realized_pnl_today: float = 0.0


class RiskGuard:
    def __init__(self, profile: BotRiskProfile | None = None):
        self.profile = profile or BotRiskProfile()
        self._killswitched: set[int] = set()  # bot_ids

    def engage_killswitch(self, bot_id: int) -> None:
        self._killswitched.add(bot_id)

    def release_killswitch(self, bot_id: int) -> None:
        self._killswitched.discard(bot_id)

    def is_killswitched(self, bot_id: int) -> bool:
        return bot_id in self._killswitched

    def check(self, bot_id: int, intent, ref_price: float, wallet: WalletState) -> list[str]:
        """Returns violations; empty list = cleared to sign."""
        from order_model import OrderIntent

        violations = list(intent.validate(ref_price))
        if self.is_killswitched(bot_id):
            violations.append("killswitch engaged")
        notional = intent.notional(ref_price)
        if notional > self.profile.max_notional_usd:
            violations.append(f"notional ${notional:,.0f} > cap ${self.profile.max_notional_usd:,.0f}")
        if self.profile.max_exposure_pct > 0:
            limit = wallet.usd_balance * self.profile.max_exposure_pct / 100
            if wallet.open_exposure_usd + notional > limit:
                violations.append(
                    f"exposure ${wallet.open_exposure_usd + notional:,.0f} > {self.profile.max_exposure_pct:.0f}% "
                    f"of ${wallet.usd_balance:,.0f} (${limit:,.0f})"
                )
        if intent.leverage > min(self.profile.max_leverage, intent.venue_cap()):
            violations.append(f"leverage {intent.leverage}x > profile/venue cap")
        if self.profile.require_stop and intent.leverage > 1:
            if not intent.stop_loss:
                violations.append("mandatory stop-loss missing on leveraged open")
            elif ref_price > 0:
                pct = abs(intent.stop_loss - ref_price) / ref_price * 100
                if pct < self.profile.min_stop_pct:
                    violations.append(f"stop too tight ({pct:.1f}% < {self.profile.min_stop_pct}%)")
                if pct > self.profile.max_stop_pct:
                    violations.append(f"stop too wide ({pct:.1f}% > {self.profile.max_stop_pct}%)")
        if wallet.open_positions >= self.profile.max_open_positions:
            violations.append("max open positions reached")
        if wallet.realized_pnl_today < 0:
            loss_pct = abs(wallet.realized_pnl_today) / max(wallet.usd_balance, 1) * 100
            if loss_pct >= self.profile.daily_loss_halt_pct:
                violations.append(
                    f"daily loss halt: -{loss_pct:.1f}% >= {self.profile.daily_loss_halt_pct:.0f}%"
                )
        return violations