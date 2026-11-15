// The only module the screens import from the core.
//
// Each function returns what the matching Python function returned from the
// server (same keys, same meaning), worked out in the browser from public
// market data. A source that cannot be read rejects with a DataError whose
// message can be shown as it is.

import { browserStorage } from "./barStorage";
import * as md from "./marketData";
import { DataError, configure, runtime } from "./runtime";
import * as terminal from "./terminal";
import * as terminalAdv from "./terminalAdv";
import type {
  AdvancedSnapshot, ChartData, ChartInterval, OrderBook, Scoreboard, TerminalSnapshot, Week,
} from "./types";
import * as wc from "./weeklyCalls";

export { DataError, configure };
export type * from "./types";

/** Leverage choices for the scoreboard; 0 means "the suggested level". */
export const LEVERAGES = wc.LEVERAGES;

// In a browser, keep price history between visits. Tests swap this out with configure().
if (!runtime.storage) runtime.storage = browserStorage();

function checkStake(stake: number): void {
  if (!(stake > 0 && stake <= 1_000_000_000)) throw new DataError("Enter the weekly amount in USD.");
}

async function checkMarket(symbol: string): Promise<void> {
  if (!(await md.catalogue()).has(symbol)) throw new DataError("That market is not available.");
}

/** This week's calls: long, short or stay out. */
export async function getWeek(): Promise<Week> {
  return wc.thisWeek();
}

/** The last finished weeks at a weekly amount and leverage (0 = the suggested level). */
export async function getScoreboard(stake: number, leverage: number): Promise<Scoreboard> {
  checkStake(stake);
  if (!(LEVERAGES as readonly number[]).includes(leverage)) {
    throw new DataError("Choose one of the listed leverage levels.");
  }
  return wc.scoreboard(stake, leverage);
}

/** The basic terminal: model positions, advice, and the market dashboard. */
export async function getTerminal(stake: number): Promise<TerminalSnapshot> {
  checkStake(stake);
  return terminal.snapshot(stake);
}

/**
 * The advanced terminal's panels. Returns with whatever is ready: worksheet
 * rows appear as each market's history arrives, and `pending` counts the rest.
 */
export async function getAdvanced(stake: number): Promise<AdvancedSnapshot> {
  checkStake(stake);
  return terminalAdv.snapshot(stake);
}

/** Candles for the chart, with the two averages the trend rule uses. */
export async function getChart(symbol: string, interval: ChartInterval): Promise<ChartData> {
  if (!(interval in terminal.CHART_BARS)) throw new DataError("Choose 1h, 4h or 1d.");
  await checkMarket(symbol);
  return terminal.chart(symbol, interval);
}

/** The order book, for when the live stream is down. */
export async function getBook(symbol: string): Promise<OrderBook> {
  await checkMarket(symbol);
  return md.orderBook(symbol);
}
