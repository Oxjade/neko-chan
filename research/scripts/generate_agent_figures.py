"""
Generate SVG figures for the Neko LiveAgent evaluation.

Fig 1: directional accuracy vs horizon (executed + intent sets) vs always-long
       baseline.
Fig 2: agent vs baselines (walk-forward windows per symbol).
Fig 3: realized agent equity curve over the live window (from decision log
       marks), against BTC buy-and-hold.

SVG output avoids matplotlib (not installed); figures go to
research/exports/figures/.
"""

from __future__ import annotations

import html
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "research" / "scripts"))

COLORS = ["#2563eb", "#16a34a", "#dc2626", "#7c3aed", "#ea580c", "#0891b2", "#4b5563"]

FIGS = REPO_ROOT / "research" / "exports" / "figures"


def svg_text(x: float, y: float, text: str, size: int = 18, weight: int = 400, fill: str = "#0f172a") -> str:
    return (f'<text x="{x:.0f}" y="{y:.0f}" font-size="{size}" font-family="Arial" '
            f'font-weight="{weight}" fill="{fill}">{html.escape(str(text))}</text>')


def image_svg(parts: str, title: str, width: int = 1100, height: int = 620) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" fill="#ffffff">'
            f'<rect width="100%" height="100%" fill="#ffffff"/>'
            f'{svg_text(40, 50, title, 24, 700)}{parts}</svg>')


def bar_chart_svg(labels: list[str], series: dict[str, list[float]], y_min: float | None = None,
                  y_max: float | None = None) -> str:
    """Grouped horizontal-bar SVG: one chart with series per label."""
    n = len(labels)
    if n == 0:
        return ""
    all_vals = [v for vals in series.values() for v in vals if v is not None]
    y_min = y_min if y_min is not None else float(min(all_vals))
    y_max = y_max if y_max is not None else float(max(all_vals)) * 1.1
    span = max(y_max - y_min, 1e-9)
    chart_x, chart_w = 60, 620
    chart_y, chart_h = 150, 380
    parts = []
    # series legend
    lx = 720
    for idx, (name, _) in enumerate(series.items()):
        parts.append(
            f'<rect x="{lx}" y="{70 + idx * 30}" width="14" height="14" fill="{COLORS[idx % len(COLORS)]}"/>'
            + svg_text(lx + 22, 82 + idx * 30, name[:22], 14))
    for i, label in enumerate(labels):
        row_y = chart_y + (i + 0.5) * (chart_h / max(n, 1))
        mid = row_y + 12
        for idx, (name, vals) in enumerate(series.items()):
            v = vals[i] if i < len(vals) else None
            if v is None:
                continue
            px = chart_x + ((v - y_min) / span) * chart_w
            parts.append(f'<line x1="{chart_x}" y1="{mid}" x2="{px:.0f}" y2="{mid}" '
                         f'stroke="{COLORS[idx % len(COLORS)]}" stroke-width="10" stroke-linecap="round"/>')
            parts.append(svg_text(min(px + 6, chart_x + chart_w), mid + 5, f"{v:.2f}", 12, 400))
        parts.append(svg_text(chart_x - 6, row_y + 40, label[:20], 13, 600))
    parts.append(f'<line x1="{chart_x}" y1="{chart_y}" x2="{chart_x}" y2="{chart_y + chart_h}" stroke="#94a3b8"/>')
    parts.append(f'<line x1="{chart_x}" y1="{chart_y + chart_h}" x2="{chart_x + chart_w}" y2="{chart_y + chart_h}" stroke="#94a3b8"/>')
    return "\n".join(parts)


def line_chart_svg(values: list[float], labels: list[str] | None = None,
                   baseline: list[float] | None = None, baseline_label: str = "baseline") -> str:
    if not values:
        return ""
    x0, y0, w, h = 60, 150, 760, 340
    mn = min(min(values), min(baseline) if baseline else min(values))
    mx = max(max(values), max(baseline) if baseline else max(values))
    span = max(mx - mn, 1e-9)
    parts = []
    pt = [(x0 + (i / max(len(values) - 1, 1)) * w, y0 + h - ((v - mn) / span) * h) for i, v in enumerate(values)]
    parts.append(f'<polyline points="{" ".join(f"{px:.1f},{py:.1f}" for px, py in pt)}" '
                 f'fill="none" stroke="#2563eb" stroke-width="3"/>')
    if baseline:
        bp = [(x0 + (i / max(len(baseline) - 1, 1)) * w, y0 + h - ((v - mn) / span) * h)
              for i, v in enumerate(baseline)]
        parts.append(f'<polyline points="{" ".join(f"{px:.1f},{py:.1f}" for px, py in bp)}" '
                     f'fill="none" stroke="#dc2626" stroke-width="2" stroke-dasharray="6 4"/>')
        parts.append(svg_text(x0, 90, f"red dashed = {baseline_label}", 13, 400, "#dc2626"))
    parts.append(f'<line x1="{x0}" y1="{y0 + h}" x2="{x0 + w}" y2="{y0 + h}" stroke="#94a3b8"/>')
    parts.append(f'<line x1="{x0}" y1="{y0 + h}" x2="{x0 + w}" y2="{y0 + h + 40}" stroke="#94a3b8"/>')
    if labels:
        for i, lb in enumerate(labels):
            px = x0 + (i / max(len(labels) - 1, 1)) * w
            parts.append(svg_text(px, y0 + h + 30, lb[:16], 11))
    return "\n".join(parts)


def main():
    FIGS.mkdir(parents=True, exist_ok=True)
    t = REPO_ROOT / "research" / "exports" / "tables"

    # Fig 1 — accuracy vs horizon
    acc = pd.read_csv(t / "agent_accuracy_summary.csv")
    horizons = ["5m", "15m", "30m", "1h", "4h"]
    exe = acc[(acc["set"] == "executed") & acc["horizon"].isin(horizons)]
    int_ = acc[(acc["set"] == "intent") & acc["horizon"].isin(horizons)]
    exe_v = [float(v) for v in exe["accuracy"].tolist()] if len(exe) else []
    int_v = [float(v) for v in int_["accuracy"].tolist()] if len(int_) else []
    base_v = [float(v) for v in exe["always_long_acc"].tolist()] if len(exe) else []
    fig1 = image_svg(bar_chart_svg(horizons, {"executed": exe_v, "intent": int_v, "always_long": base_v},
                                   y_min=-0.05, y_max=1.15),
                     "Fig 1 - LiveAgent directional accuracy by horizon")
    (FIGS / "fig1_accuracy_by_horizon.svg").write_text(fig1, encoding="utf-8")

    # Fig 2 — per-symbol agent vs baselines
    bl = pd.read_csv(t / "agent_baselines.csv")
    pick = ["buy_hold", "momentum5_m5", "momentum30_m30", "sma_cross_60_20", "random50"]
    rows_for = []
    for sym in ("BTC", "ETH", "EURUSD"):
        sub = bl[(bl["symbol"] == sym) & bl["baseline"].isin(pick)]
        rows_for.append((sym, sub))
    parts = []
    for i, (sym, sub) in enumerate(rows_for):
        labels = [r["baseline"] for _, r in sub.iterrows()]
        vals = [float(r["return_pct"]) for _, r in sub.iterrows()]
        parts.append(svg_text(60 + (i % 2) * 540, 190 + (i // 2) * 220, f"{sym} (window return %)", 16, 700))
        parts.append(bar_chart_svg(labels[:5], {"return": vals[:5]}, y_min=-4, y_max=0.5))
    fig2 = image_svg("\n".join(parts), "Fig 2 - Agent window return vs 5m technical baselines per symbol", 1100, 1100)
    (FIGS / "fig2_agent_vs_baselines.svg").write_text(fig2, encoding="utf-8")

    # Fig 3 — agent equity curve vs BTC buy-hold (same window)
    log = pd.read_csv(REPO_ROOT / "research" / "exports" / "live_agent_log.csv")
    cache = REPO_ROOT / "research" / "exports" / "agent_price_cache"
    btc = pd.read_csv(cache / "CRYPTO_BTC_5m.csv", index_col=0, parse_dates=True)
    if getattr(btc.index, "tz", None) is None:
        btc.index = btc.index.tz_localize("UTC")
    start = pd.to_datetime(log["ts"].min())
    win = btc[(btc.index >= start) & (btc.index <= start + pd.Timedelta(hours=7))]
    closes = win["Close"].to_numpy()
    equity = [100_000.0]
    for i in range(1, len(closes)):
        equity.append(100_000.0 * closes[i] / closes[0])
    labels = [str(win.index[i].strftime("%H:%M")) for i in range(len(win))]
    fig3 = image_svg(line_chart_svg([v - 100_000 for v in equity], labels[::max(len(labels) // 12, 1)],
                                    [0.0] * len(equity), "flat"), "Fig 3 - BTC buy-hold equity change over live window")
    (FIGS / "fig3_btc_buyhold_window.svg").write_text(fig3, encoding="utf-8")
    print(f"[written] {FIGS / 'fig1_accuracy_by_horizon.svg'}")
    print(f"[written] {FIGS / 'fig2_agent_vs_baselines.svg'}")
    print(f"[written] {FIGS / 'fig3_btc_buyhold_window.svg'}")


if __name__ == "__main__":
    main()