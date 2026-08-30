"""Gate G5: Fee is charged at entry only, never at close (no double-deduction)."""
import re

src = open("service/server/routes_signals.py").read()
bad = [l.strip() for l in src.split("\n") if re.search(r"(trade_value - fee|credit = qty \* .* - fee|cover_credit = .* - fee|follower_net = .* - fee)", l)]
print("exit-side fee refs:", len(bad))
assert len(bad) == 0, f"found {len(bad)} exit-side fee refs: {bad}"
print("FEE MODEL: PASS")