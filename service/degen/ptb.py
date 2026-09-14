"""Pure PTB composition for Suipump degen trades (§3.2a / §3.6 fee legs).

Produces (inputs, commands) BCS byte lists for
`sui_adapter.SUIAdapter._broadcast_ptb(inputs, commands, gas_price, budget,
gas_coin=...)`, which owns gas selection, signing and broadcast with the wallet
key. This module does NOT reimplement crypto — it only composes commands, using
the exact BCS argument/command helpers imported from the adapter so bytes match
what a node will parse.

Verified facts (mainnet disassembly):
  buy<T>(curve, Coin<SUI>, min_tokens_out u64, Option<address>, &PriceConfig,
         &Clock) -> (Coin<T>, Coin<SUI> change)   [2 return values → NestedResult]
  sell<T>(curve, Coin<T>, min_sui_out u64, Option<address>) -> Coin<SUI>
        [1 return value → plain Result]
Argument tags: Input(0x01+u16), Result(0x02+u16)=whole-vector-or-single,
NestedResult(0x03+u16 cmd+u16 idx) for multi-value commands.

Fee = atomic in-PTB leg (SplitCoins+TransferObjects), sized off-chain from the
pre-trade quote (`fee_atoms`/`fee_sui_mist` are Pure inputs). Never a gas-budget
redirect — budget is a cap, not a payment (§3.2a).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "execution"))

from sui_adapter import (  # reuse exact primitives; no crypto duplication
    _bcs_addr, _bcs_addr_padded, _bcs_u64,
    _bcs_call_arg_shared, _bcs_call_arg_imm_or_owned, _bcs_call_arg_pure,
    _bcs_command_move_call, _bcs_command_split_coins,
    _bcs_command_transfer_objects,
)

from . import constants as K


def _norm(addr: str) -> str:
    a = addr[2:] if addr.startswith("0x") else addr
    return "0x" + a.zfill(64)


def _objref(object_id: str, version: int, digest_hex: str) -> bytes:
    # _bcs_object_ref wants a 32-byte address; normalize first
    return _bcs_call_arg_imm_or_owned(_norm(object_id), int(version), digest_hex)


# ---- Argument builders (the missing NestedResult + typed helpers) ----
def arg_input(i: int) -> bytes:            # Argument::Input(u16)
    return b"\x01" + i.to_bytes(2, "little")


def arg_result(cmd: int) -> bytes:         # Argument::Result(u16) — single-or-vector
    return b"\x02" + cmd.to_bytes(2, "little")


def arg_nested(cmd: int, idx: int) -> bytes:  # Argument::NestedResult(u16, u16)
    return b"\x03" + cmd.to_bytes(2, "little") + idx.to_bytes(2, "little")


# ---- Value (CallArg) builders ----
def pure_u64(v: int) -> bytes:
    return _bcs_call_arg_pure(_bcs_u64(int(v)))


def pure_addr(addr: str) -> bytes:
    return _bcs_call_arg_pure(_bcs_addr_padded(addr))


def pure_option_none() -> bytes:
    return _bcs_call_arg_pure(b"")


def pure_option_addr(addr: str) -> bytes:
    return _bcs_call_arg_pure(K.OPTION_ADDR_PREFIX + _bcs_addr_padded(addr))


def shared(object_id: str, initial_shared_version: int, mutable: bool) -> bytes:
    return _bcs_call_arg_shared(_norm(object_id), int(initial_shared_version), mutable)


def owned(object_id: str, version: int, digest_hex: str) -> bytes:
    return _objref(object_id, version, digest_hex)


def _movecall(pkg, module, fn, type_args, args):
    return _bcs_command_move_call(
        {"package": pkg, "module": module, "function": fn, "type_arguments": list(type_args)},
        args)


def _pkg_for(token_type: str) -> str:
    a = token_type.split("::")[0]
    core = a[2:] if a.startswith("0x") else a
    return "0x" + core.zfill(64).lower()


# ----------------------------------------------------------------- BUY
# inputs: 0 coin, 1 curve, 2 min_out, 3 referrer, 4 price_config, 5 clock,
#         6 fee_atoms, 7 fee_recipient, 8 user_addr
# cmds:   0 buy -> (tokens, change)
#         1 SplitCoins(tokens=Nested(0,0), [fee_atoms]) -> vector<Coin<T>>
#         2 Transfer(Nested(1,0)) -> fee_recipient
#         3 Transfer([Nested(1,1), Nested(0,1)]) -> user
def build_buy(curve_id: str, curve_isv: int, token_type: str,
              buy_coin: tuple, min_out: int, *, user_addr: str,
              fee_atoms: int = 0, fee_recipient: str = "",
              referrer: str = "") -> tuple:
    pkg = _pkg_for(token_type)
    inputs = [
        owned(*buy_coin),
        shared(curve_id, curve_isv, True),
        pure_u64(min_out),
        pure_option_addr(referrer) if referrer else pure_option_none(),
        shared(K.SUIPUMP_PRICE_CONFIG, K.PRICE_CONFIG_ISV, False),
        shared(K.SUI_CLOCK, 1, False),
    ]
    cmds = [_movecall(pkg, K.SUIPUMP_MODULE, "buy", [token_type],
                      [arg_input(0), arg_input(1), arg_input(2),
                       arg_input(3), arg_input(4), arg_input(5)])]
    if fee_atoms > 0 and fee_recipient:
        inputs += [pure_u64(fee_atoms), pure_addr(fee_recipient), pure_addr(user_addr)]
        cmds += [
            _bcs_command_split_coins(arg_nested(0, 0), [arg_input(6)]),
            _bcs_command_transfer_objects([arg_nested(1, 0)], arg_input(7)),
            _bcs_command_transfer_objects([arg_nested(1, 1), arg_nested(0, 1)],
                                          arg_input(8)),
        ]
    else:
        inputs += [pure_addr(user_addr)]
        cmds += [_bcs_command_transfer_objects([arg_nested(0, 0), arg_nested(0, 1)],
                                               arg_input(6))]
    return inputs, cmds


# ----------------------------------------------------------------- SELL
# inputs: 0 token coin, 1 curve, 2 min_sui, 3 referrer, 4 fee_mist,
#         5 fee_recipient, 6 user_addr
# cmds:   0 sell -> Coin<SUI>
#         1 SplitCoins(Result(0), [fee])  (fee>0)
#         2 Transfer(Nested(1,0)) -> fee_recipient
#         3 Transfer(Nested(1,1)) -> user      (fee>0)  else Transfer(Result(0))->user
def build_sell(curve_id: str, curve_isv: int, token_type: str,
               sell_coin: tuple, min_sui_out: int, *, user_addr: str,
               fee_sui_mist: int = 0, fee_recipient: str = "",
               referrer: str = "") -> tuple:
    pkg = _pkg_for(token_type)
    inputs = [
        owned(*sell_coin),
        shared(curve_id, curve_isv, True),
        pure_u64(min_sui_out),
        pure_option_addr(referrer) if referrer else pure_option_none(),
    ]
    cmds = [_movecall(pkg, K.SUIPUMP_MODULE, "sell", [token_type],
                      [arg_input(1), arg_input(0), arg_input(2), arg_input(3)])]
    if fee_sui_mist > 0 and fee_recipient:
        inputs += [pure_u64(fee_sui_mist), pure_addr(fee_recipient), pure_addr(user_addr)]
        cmds += [
            _bcs_command_split_coins(arg_result(0), [arg_input(4)]),
            _bcs_command_transfer_objects([arg_nested(1, 0)], arg_input(5)),
            _bcs_command_transfer_objects([arg_nested(1, 1)], arg_input(6)),
        ]
    else:
        inputs += [pure_addr(user_addr)]
        cmds += [_bcs_command_transfer_objects([arg_result(0)], arg_input(4))]
    return inputs, cmds


# ----------------------------------------------------------------- FUND split (§3.6)
# split one owned SUI coin into N equal legs + change to self. Used by the
# spread-burst funder and the gas/buy-pool janitor.
# inputs: 0 source coin, 1 gas coin, 2 leg amount, 3..: leg addresses, last: self
def build_split_fund(source_coin: tuple, leg_amount: int, recipients: list,
                     self_addr: str, *, gas_coin: tuple | None = None) -> tuple:
    """SplitCoins(source, [amt]*len(recipients)) then transfer each; change to self.
    NOTE: source may equal gas only if gas is a *different* coin (executor picks)."""
    inputs = [owned(*source_coin), pure_u64(leg_amount)]
    for r in recipients:
        inputs.append(pure_addr(r))
    inputs.append(pure_addr(self_addr))
    n = len(recipients)
    amounts = [arg_input(1)] * n
    cmds = [_bcs_command_split_coins(arg_input(0), amounts)]
    for i in range(n):
        cmds.append(_bcs_command_transfer_objects([arg_nested(0, i)],
                                                  arg_input(2 + i)))
    # remainder (the source coin, drained) -> self
    cmds.append(_bcs_command_transfer_objects([arg_input(0)],
                                              arg_input(2 + n)))
    return inputs, cmds
