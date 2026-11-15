"""Small reference answers from the Python side, for the unit tests.

Writes tests/fixtures/reference.json (committed):

  sums          how NumPy adds, averages and spreads arrays of several lengths
  python_sums   how Python's own sum() adds lists of floats
  formats       how Python writes numbers as text (rounding of exact ties)
  correlation   the random walk test_terminal_adv.py uses for its correlation test
  replay        the trend rule replayed over the saved BTC and SUI price history
                by desk.replay, beside research/scripts/strategy_search.py

Run with the repository's Python environment:

    .venv/bin/python neko-lite/tests/fixtures/make_reference.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for path in (REPO / "service" / "server", REPO / "research" / "scripts"):
    sys.path.insert(0, str(path))

import desk  # noqa: E402
import strategy_search as ss  # noqa: E402


def sums() -> list[dict]:
    rng = np.random.default_rng(11)
    out = []
    for n in (1, 2, 7, 8, 9, 14, 16, 17, 42, 128, 129, 180, 300):
        values = rng.normal(0.0, 0.01, n) + (0.1 if n % 2 else 0.0)
        out.append({"values": values.tolist(), "sum": float(values.sum()), "mean": float(values.mean()),
                    "std": float(values.std(ddof=1)) if n > 1 else None})
    return out


def python_sums() -> list[dict]:
    """Python's own sum() (compensated since 3.12), which is not how NumPy adds."""
    rng = np.random.default_rng(13)
    out = []
    for n, scale in ((1, 1.0), (2, 1.0), (5, 1e6), (26, 0.05), (40, 30.0), (200, 1e-3)):
        values = [float(v) for v in rng.normal(0.0, scale, n)]
        if n > 4:
            values[2] = values[2] * 1e9          # a wide range of sizes is where plain adding drifts
        out.append({"values": values, "sum": sum(values)})
    return out


def formats() -> list[list]:
    cases = [(0.5, 0), (1.5, 0), (2.5, 0), (3.5, 0), (0.25, 1), (0.35, 1), (0.75, 1), (2.675, 2), (1.005, 2),
             (0.125, 2), (0.375, 2), (-0.04, 1), (-0.0, 1), (0.0, 1), (14.606265876375957, 1), (64925.0, 0),
             (1234567.891, 2), (1234.5, 0), (1235.5, 0), (999.9996, 2), (0.00012345, 4), (0.72558, 4),
             (2309.902217939488, 0), (86.502, 2), (-1.959695893310754, 2), (12.5, 0), (13.5, 0), (2.5 / 6, 0)]
    return [[v, d, format(v, f".{d}f"), format(v, f",.{d}f")] for v, d in cases]


def bars(coin: str) -> dict:
    df = ss.load(coin, "4h")
    t = np.array([int(stamp.timestamp() * 1000) for stamp in df["t"]], dtype=np.int64)
    return {"t": t, "open": df["open"].to_numpy(float), "high": df["high"].to_numpy(float),
            "low": df["low"].to_numpy(float), "close": df["close"].to_numpy(float)}, df


def replay() -> dict:
    out = {}
    for coin in ("BTC", "SUI"):
        b, df = bars(coin)
        full = desk.replay(b, warmup=1)
        settled = desk.replay(b)
        theirs = ss.simulate(df, ss.ema_trend(df, 60, 240, 6), False)
        state = {k: settled[k] for k in ("open", "pending", "bars", "trend", "trend_days", "atr_pct",
                                         "last_close", "last_bar")}
        out[coin] = {
            "csv": f"research/exports/price_history/{coin}_4h.csv",
            "first_bar_ms": int(b["t"][0]), "bars": len(b["t"]),
            "trades_from_first_bar": full["trades"],
            "trades_after_warmup": settled["trades"],
            "state": state,
            "research_gross": [float(row["gross"]) for row in theirs],
            "trust": {d: desk.trust(settled["trades"], d) for d in ("long", "short")},
        }
    return out


def main():
    rng = np.random.default_rng(7)
    base = np.exp(np.cumsum(rng.normal(0, 0.01, 200)))
    reference = {"sums": sums(), "python_sums": python_sums(), "formats": formats(),
                 "correlation": {"base": base.tolist()}, "replay": replay()}
    target = HERE / "reference.json"
    target.write_text(json.dumps(reference, allow_nan=False))
    print(f"wrote {target} ({target.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
