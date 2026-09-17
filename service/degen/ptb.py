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
    _bcs_addr, _bcs_addr_padded, _bcs_u64, _bcs_vec,
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
    return _bcs_call_arg_pure(b"\x00")  # BCS Option::None = single tag byte 0x00


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
              referrer: str = "", buy_from_gas: bool = False,
              buy_atoms: int = 0, pkg: str = "") -> tuple:
    # `bonding_curve::buy` lives in the LAUNCHPAD package, not the token's own
    # package (a Suipump token package only has `suipump::init`). Callers pass
    # the curve's package via launchpad.curve_pkg(curve_type); fall back to the
    # token package for launchpads where the two coincide.
    pkg = pkg or _pkg_for(token_type)
    if buy_from_gas:
        # Single SUI coin (gas == value): derive the buy Coin<SUI> from the gas
        # coin via SplitCoins(GasCoin, [buy_atoms]) — the canonical gas-smashing
        # pattern. Referencing the same ObjectRef as both an owned input and the
        # gas payment is rejected by the node ("Invalid withdraw reservation",
        # 2x balance reservation), so the coin must NOT be an owned input here.
        inputs = [
            shared(curve_id, curve_isv, True),                             # 0
            pure_u64(min_out),                                             # 1
            pure_option_addr(referrer) if referrer else pure_option_none(),  # 2
            shared(K.SUIPUMP_PRICE_CONFIG, K.PRICE_CONFIG_ISV, False),     # 3
            shared(K.SUI_CLOCK, 1, False),                                 # 4
            pure_u64(int(buy_atoms)),                                      # 5
        ]
        cmds = [_bcs_command_split_coins(b"\x00", [arg_input(5)])]         # 0 -> Result(0)
        cmds.append(_movecall(pkg, K.SUIPUMP_MODULE, "buy", [token_type],
                              [arg_input(0), arg_nested(0, 0), arg_input(1),
                               arg_input(2), arg_input(3), arg_input(4)]))  # 1
        buy_res = 1
        if fee_atoms > 0 and fee_recipient:
            inputs += [pure_u64(fee_atoms), pure_addr(fee_recipient), pure_addr(user_addr)]
            cmds += [
                _bcs_command_split_coins(arg_nested(buy_res, 0), [arg_input(6)]),  # 2
                _bcs_command_transfer_objects([arg_nested(2, 0)], arg_input(7)),
                # remainder tokens stay in Nested(buy_res,0); SUI change is
                # Nested(buy_res,1). Nested(2,1) does not exist (1-element split).
                _bcs_command_transfer_objects([arg_nested(buy_res, 0), arg_nested(buy_res, 1)],
                                              arg_input(8)),
            ]
        else:
            inputs += [pure_addr(user_addr)]
            cmds += [_bcs_command_transfer_objects(
                [arg_nested(buy_res, 0), arg_nested(buy_res, 1)], arg_input(6))]
        return inputs, cmds
    inputs = [
        owned(*buy_coin),
        shared(curve_id, curve_isv, True),
        pure_u64(min_out),
        pure_option_addr(referrer) if referrer else pure_option_none(),
        shared(K.SUIPUMP_PRICE_CONFIG, K.PRICE_CONFIG_ISV, False),
        shared(K.SUI_CLOCK, 1, False),
    ]
    cmds = [_movecall(pkg, K.SUIPUMP_MODULE, "buy", [token_type],
                      [arg_input(1), arg_input(0), arg_input(2),
                       arg_input(3), arg_input(4), arg_input(5)])]
    if fee_atoms > 0 and fee_recipient:
        inputs += [pure_u64(fee_atoms), pure_addr(fee_recipient), pure_addr(user_addr)]
        cmds += [
            _bcs_command_split_coins(arg_nested(0, 0), [arg_input(6)]),
            _bcs_command_transfer_objects([arg_nested(1, 0)], arg_input(7)),
            # remainder tokens stay in Nested(0,0); SUI change is Nested(0,1).
            _bcs_command_transfer_objects([arg_nested(0, 0), arg_nested(0, 1)],
                                          arg_input(8)),
        ]
    else:
        inputs += [pure_addr(user_addr)]
        cmds += [_bcs_command_transfer_objects([arg_nested(0, 0), arg_nested(0, 1)],
                                               arg_input(6))]
    return inputs, cmds


# ----------------------------------------------------------------- SELL
def build_sell(curve_id: str, curve_isv: int, token_type: str,
               sell_coin: tuple, min_sui_out: int, *, user_addr: str,
               fee_sui_mist: int = 0, fee_recipient: str = "",
               referrer: str = "", pkg: str = "",
               extra_coins: list | None = None,
               sell_atoms: int | None = None) -> tuple:
    pkg = pkg or _pkg_for(token_type)
    inputs = [owned(*sell_coin)]
    cmds = []
    extra = list(extra_coins or [])
    for c in extra:
        inputs.append(owned(*c))
    if extra:
        cmds.append(b"\x03" + arg_input(0)
                    + _bcs_vec([arg_input(i) for i in range(1, len(inputs))]))
    token_arg = arg_input(0)
    remainder_arg = None
    if sell_atoms is not None:
        if type(sell_atoms) is not int or not 0 < sell_atoms < 2 ** 64:
            raise ValueError("sell_atoms must be a positive u64 integer")
        inputs.append(pure_u64(sell_atoms))
        cmds.append(_bcs_command_split_coins(arg_input(0), [arg_input(len(inputs) - 1)]))
        token_arg = arg_nested(len(cmds) - 1, 0)
        remainder_arg = arg_input(0)
    curve_idx = len(inputs)
    inputs += [
        shared(curve_id, curve_isv, True),
        pure_u64(min_sui_out),
        pure_option_addr(referrer) if referrer else pure_option_none(),
    ]
    cmds.append(_movecall(pkg, K.SUIPUMP_MODULE, "sell", [token_type],
                          [arg_input(curve_idx), token_arg,
                           arg_input(curve_idx + 1), arg_input(curve_idx + 2)]))
    sell_cmd = len(cmds) - 1
    if fee_sui_mist > 0 and fee_recipient:
        inputs += [pure_u64(fee_sui_mist), pure_addr(fee_recipient), pure_addr(user_addr)]
        user_idx = curve_idx + 5
        cmds += [
            _bcs_command_split_coins(arg_result(sell_cmd), [arg_input(curve_idx + 3)]),
            _bcs_command_transfer_objects([arg_nested(sell_cmd + 1, 0)],
                                          arg_input(curve_idx + 4)),
            # the sell coin's remainder stays in Result(sell_cmd) after the split.
            _bcs_command_transfer_objects([arg_result(sell_cmd)], arg_input(user_idx)),
        ]
    else:
        inputs += [pure_addr(user_addr)]
        user_idx = curve_idx + 3
        cmds += [_bcs_command_transfer_objects([arg_result(sell_cmd)],
                                               arg_input(user_idx))]
    if remainder_arg is not None:      # hand the unsold tokens back to the wallet
        cmds.append(_bcs_command_transfer_objects([remainder_arg], arg_input(user_idx)))
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
