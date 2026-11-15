// Small helpers shared by the terminal's basic and advanced screens.

import type { AdvancedSnapshot, Candle, ChartData, OrderBook, TerminalSnapshot } from "../core/api";

export const COLORS = { up: "#22e07a", down: "#ff4d5e", amber: "#ffa028", cyan: "#35d6e6", grid: "#12262c",
                        text: "#8296a3", ink: "#d9e4ea", bg: "#0a1016", violet: "#b48cff" };
export const SERIES = ["#ffa028", "#35d6e6", "#b48cff", "#22e07a", "#ff7ab6", "#d9e4ea", "#ffe45c", "#ff4d5e"];

type Maybe = number | null | undefined;

export function px(v: Maybe): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const digits = v >= 1000 ? 1 : v >= 10 ? 2 : 4;
  return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function pct(v: Maybe, digits = 2): string {
  return v === null || v === undefined ? "–" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

export function usd(v: Maybe): string {
  if (v === null || v === undefined) return "–";
  return `${v < 0 ? "-" : "+"}$${Math.abs(v).toFixed(2)}`;
}

export function compact(v: Maybe): string {
  if (v === null || v === undefined) return "–";
  const abs = Math.abs(v);
  if (abs >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${(v / 1e3).toFixed(0)}K`;
  return v.toFixed(0);
}

export function tone(v: Maybe): string {
  return (v as number) > 0 ? "up" : (v as number) < 0 ? "down" : "";
}

// Background for a heat-map cell: green above zero, red below, stronger further out.
export function heat(value: Maybe, full: number): string {
  if (value === null || value === undefined || !full) return "";
  const strength = Math.min(1, Math.abs(value) / full) * 0.5;
  const rgb = value >= 0 ? "34,224,122" : "255,77,94";
  return `background:rgba(${rgb},${strength.toFixed(3)})`;
}

// ---- what the two screens share ------------------------------------------------

/** "1s" is the chart built in the browser from the live stream; the others come from the exchange. */
export type Interval = "1s" | ChartData["interval"];

export interface TapeTrade {
  time: number;
  px: number;
  sz: number;
  buy: boolean;
}

export interface TerminalState {
  snap: TerminalSnapshot | null;
  chart: ChartData | null;
  selected: string;
  interval: Interval;
  stake: number;
  prices: Record<string, number>;
  stream: "connecting" | "live" | "poll";
  lastPoll: number;
  error: string | null;
  log: Array<{ time: string; title: string }>;
  seen: Set<string>;
  hover: number | null;
  frame: number | null;
  mode: "basic" | "advanced";
  adv: AdvancedSnapshot | null;
  book: OrderBook | null;
  tape: TapeTrade[];
  bookSymbol: string | null;
  tradeSymbol: string | null;
  tradeBacklog: boolean;
  watch: Set<string>;
}

/** The parts of the page the painters fill, by name. */
export interface Elements {
  [name: string]: HTMLElement | undefined;
  canvas?: HTMLCanvasElement;
}

export interface Helpers {
  select: (symbol: string) => void;
  live: (symbol: string, fallback: Maybe) => Maybe;
}

export type { Candle };
