// Shapes of the data the screens read. The names and meanings are those of
// the Python reference (service/server), so a field here is the same field
// there: snake_case keys, null where Python gives None.

export type ChartInterval = "1h" | "4h" | "1d";
export type Side = "long" | "short";
export type TrendDirection = "up" | "down";
export type AssetClass = "crypto" | "stocks" | "indices" | "forex" | "commodities";

// ---- markets -------------------------------------------------------------------

/** One market with its live price (market_data.catalogue). */
export interface Market {
  symbol: string;
  label: string;
  class: AssetClass;
  price: number;
  day_change_pct: number | null;
  /** The price today's change is measured from. */
  prev_day_price: number | null;
  funding_hourly: number;
  max_leverage: number;
  day_volume_usd: number;
  open_interest_usd: number;
}

export interface Candle {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

export interface BookLevel {
  px: number;
  sz: number;
}

/** market_data.order_book(symbol). The live stream builds the same shape without `time`. */
export interface OrderBook {
  symbol: string;
  bids: BookLevel[];
  asks: BookLevel[];
  time?: number | null;
}

/** terminal.chart(symbol, interval). An average is missing when the history is too short for it. */
export interface ChartData {
  symbol: string;
  label: string;
  interval: ChartInterval;
  candles: Candle[];
  emas: { "10d"?: Array<number | null>; "40d"?: Array<number | null> };
}

// ---- the tested record (research tables) ---------------------------------------

export interface HalfRecord {
  right: number | null;
  avg_pct: number | null;
  coin_weeks: number | null;
}

export interface LeverageRecord {
  weeks: number;
  ahead: number;
  liquidated: number;
  avg_per_week: number;
}

export interface SideConfig {
  allowed: boolean;
  /** null where no tested level was safe enough; such a side is never called. */
  suggested_leverage: number | null;
  first_half: HalfRecord;
  second_half: HalfRecord;
  by_leverage: Record<string, LeverageRecord>;
}

export type CallsConfig = Record<string, Record<Side, SideConfig>>;

export interface ExitRecord {
  weeks: number;
  ahead: number;
  avg: number;
  worst: number;
  total: number;
}

/** How a take-profit plan did over the tested weeks, as a share of the stake. */
export interface TakeProfitRecord extends ExitRecord {
  first_target: number;
  /** Share of weeks in which the take profit was reached. */
  final_target: number;
  stopped: number;
  locked_out: number;
}

/** The take profit for one side (crypto_take_profit.py). `target` is in normal weeks; null means hold to Monday. */
export interface TakeProfitPlan {
  target: number | null;
  with: TakeProfitRecord;
  stop_only: TakeProfitRecord;
  at_one_normal_week: TakeProfitRecord | null;
  /** Share of weeks that reached the target, where there is one. */
  reached: number | null;
  plans_as_good: number;
  plans_tested: number;
}

export type TakeProfit = Partial<Record<Side, TakeProfitPlan>>;
export type Targets = Record<Side, number | null>;

export interface SpotStats {
  weeks: number;
  grew_to: number;
  per_year_pct: number;
  worst_fall_pct: number;
  worst_week_pct: number;
  time_invested: number;
  switches_per_year: number;
  weeks_up_when_in: number;
}

export interface SpotRow {
  rule: string;
  all: SpotStats;
  first_half: SpotStats;
  second_half: SpotStats;
}

export interface SpotRecord {
  rule: SpotRow;
  hold: SpotRow;
  from: string;
  to: string;
  split: string;
}

// ---- this week (weekly_calls.this_week) ----------------------------------------

export interface MoodReading {
  value: number;
  label: string;
  date: string;
}

export interface CoinRecord {
  first_half: HalfRecord;
  second_half: HalfRecord;
  at_leverage: { ahead: number; liquidated: number; avg_per_week: number; weeks: number } | null;
}

export interface WeekCoin {
  coin: string;
  name: string;
  mood_greedy: boolean;
  four_week_change_pct: number;
  /** What the rule says before asking whether that side has earned a call. */
  signal: Side | null;
  /** The call actually made, or null for stay out. */
  side: Side | null;
  skipped: boolean;
  leverage: number | null;
  price: number | null;
  record: CoinRecord | null;
  /** For a called coin: the weekly amount at which its share becomes an order the exchange accepts. */
  min_weekly_amount?: number;
  // Present only for a called coin with a live price.
  entry?: number;
  liquidation_move_pct?: number;
  liquidation_price?: number;
  // Present only when the coin's normal week could be measured.
  normal_week_pct?: number;
  stop_pct?: number;
  stop_price?: number;
  stake_lost_at_stop?: number;
  // Present only where the call's side has a take profit.
  target_pct?: number;
  target_price?: number;
  stake_made_at_target?: number;
}

export interface WeekCall {
  coin: string;
  side: Side;
  leverage: number;
}

export interface PreviewCoin {
  coin: string;
  name: string;
  signal: Side | null;
  side: Side | null;
  skipped: boolean;
  leverage: number | null;
  four_week_change_pct: number;
}

export interface WeekPreview {
  mood: MoodReading;
  coins: PreviewCoin[];
}

export interface WeekExits {
  stop: number | null;
  targets: Targets;
  take_profit: TakeProfit;
  with: ExitRecord | null;
  holding: ExitRecord | null;
}

export interface Week {
  calls: WeekCall[];
  mood: MoodReading;
  coins: WeekCoin[];
  spot_record: SpotRecord | null;
  week_of: string;
  next_call: string;
  preview: WeekPreview | null;
  exits: WeekExits;
  /** The exchange refuses to open anything smaller (USD). */
  min_order_usd: number;
}

// ---- week by week (weekly_calls.scoreboard) ------------------------------------

export interface ScoreRow {
  week_of: string;
  mood: number;
  calls: WeekCall[];
  skipped: string[];
  wiped: string[];
  stopped: string[];
  took_profit: string[];
  /** Coins that had a call too small for the exchange to accept at this amount. */
  too_small: string[];
  result: number;
  running_total: number;
}

export interface Expectation {
  coin: string;
  side: Side;
  leverage: number;
  weeks: number;
  ahead: number;
  liquidated: number;
  avg_per_week: number;
}

export interface Scoreboard {
  stake: number;
  /** 0 means the suggested level. */
  leverage: number;
  rows: ScoreRow[];
  total: number;
  ahead: number;
  behind: number;
  no_trade: number;
  expect: Expectation[];
  /** Coins whose exchange limit is below the chosen leverage. */
  capped: Record<string, number>;
  min_order_usd: number;
}

// ---- the terminal (terminal.snapshot) ------------------------------------------

/** "target" means the take profit was reached and the trade is closed. */
export type PositionStatus = "open" | "stopped" | "liquidated" | "target";

/** A model position: what the weekly amount would be doing had the call been followed. */
export interface Position {
  coin: string;
  name: string;
  side: Side;
  leverage: number;
  share: number;
  position_usd: number;
  entry: number;
  last: number;
  stop: number | null;
  target: number | null;
  liquidation: number | null;
  status: PositionStatus;
  move_pct: number;
  pnl_usd: number;
  pnl_pct: number;
  normal_week_pct: number | null;
  // Present only while the position is open and has a take profit.
  target_distance_pct?: number;
  // Present only while the position is open and has a stop.
  stop_distance_pct?: number;
  stop_room_left?: number | null;
}

/** A call whose share of the weekly amount is below the exchange's smallest order. */
export interface TooSmall {
  coin: string;
  name: string;
  side: Side;
  leverage: number;
  position_usd: number;
  min_weekly_amount: number;
}

export type AdviceLevel = "critical" | "warn" | "good" | "info";

export interface Advice {
  level: AdviceLevel;
  key: string;
  title: string;
  text: string;
  symbol: string | null;
}

export interface Vote {
  name: string;
  direction: TrendDirection;
}

export interface Recommendation {
  symbol: string;
  lean: number;
  verdict: "LONG" | "SHORT" | "STAY OUT" | "NO CALL";
  detail: string;
  strength: number;
  of: number;
  votes: Vote[];
}

export interface TerminalMarket {
  symbol: string;
  label: string;
  class: AssetClass;
  price: number | null;
  day_change_pct: number | null;
  /** The price today's change is measured from. */
  prev_day_price: number | null;
  /** The last week of four-hour closes; empty until the market's history has loaded. */
  spark: number[];
  note: string;
  recommendation: Recommendation;
}

export interface StayOut {
  coin: string;
  name: string;
  signal: Side | null;
  skipped: boolean;
}

export interface TerminalSnapshot {
  now: string;
  week_of: string;
  next_call: string;
  mood: { week: MoodReading; today: MoodReading | null };
  stake: number;
  positions: Position[];
  week_pnl_usd: number;
  advice: Advice[];
  markets: TerminalMarket[];
  too_small: TooSmall[];
  stay_out: StayOut[];
}

// ---- the advanced screen (terminal_adv.snapshot) -------------------------------

export interface Mover {
  symbol: string;
  label: string;
  price: number;
  day_change_pct: number;
}

export interface MoverGroup {
  up: Mover[];
  down: Mover[];
  count: number;
}

export interface BreadthCount {
  up: number;
  down: number;
  flat: number;
}

export type SignalState = "waiting" | "new" | "active" | "stopped";

/** One catalogue market, as the movers and breadth need it to be counted again from live prices. */
export interface UniverseMarket {
  symbol: string;
  label: string;
  class: AssetClass;
  price: number;
  prev_day_price: number | null;
  day_volume_usd: number;
}

export interface WorksheetRow {
  symbol: string;
  label: string;
  class: AssetClass;
  week_pct: number | null;
  month_pct: number | null;
  /** The prices the week and month changes are measured from. */
  week_ref_price: number | null;
  month_ref_price: number | null;
  four_week_pct: number | null;
  trend: TrendDirection;
  trend_days: number;
  atr_pct: number | null;
  vol_7d: number | null;
  vol_30d: number | null;
  /** The trend rule's direction, or null when it is waiting, stopped or muted on this market. */
  signal: Side | null;
  signal_state: SignalState;
  record_wins: number;
  record_trades: number;
  record_avg_pct: number | null;
  price: number | null;
  day_change_pct: number | null;
  /** The price today's change is measured from. */
  prev_day_price: number | null;
  funding_apr: number;
  open_interest_usd: number | null;
  day_volume_usd: number | null;
}

export interface Correlation {
  labels: string[];
  matrix: number[][];
  bars?: number;
}

export interface SignalRow {
  coin: string;
  side: Side;
  allowed: boolean;
  leverage: number | null;
  active: boolean;
  pointed: boolean;
  right_first: number | null;
  right_second: number | null;
  avg_first: number | null;
  avg_second: number | null;
  weeks: number;
}

export interface LeverageRow {
  coin: string;
  side: Side;
  suggested: number | null;
  /** Keyed by leverage level ("3" for 3x); null where a level was never tested. */
  liquidated: Record<string, number | null>;
  avg: Record<string, number | null>;
}

export interface LeverageTable {
  levels: number[];
  rows: LeverageRow[];
}

export interface RiskRow {
  coin: string;
  side: Side;
  status: PositionStatus;
  leverage: number;
  exposure_usd: number;
  pnl_usd: number;
  stop_distance_pct: number | null;
  liquidation_distance_pct: number | null;
  loss_at_stop_usd: number;
  normal_week_pct: number | null;
}

/** The totals are missing only if the panel itself failed. */
export interface Risk {
  rows: RiskRow[];
  exposure_usd?: number;
  exposure_of_stake?: number;
  worst_case_usd?: number;
  in_cash_usd?: number;
}

export interface FundingRow {
  symbol: string;
  label: string;
  funding_apr: number;
  open_interest_usd: number;
  day_volume_usd: number;
}

/** One pair on the forex board (forex_weekly.overview). */
export interface ForexPair {
  pair: string;
  price: number;
  as_of: string;
  /** The last finished week: one Friday's fixing to the next. */
  last_week_pct: number;
  /** The week being traded now, against the last Friday close; null when nothing has been fixed since. */
  this_week_pct: number | null;
  normal_week_pct: number;
  /** One pip in price terms: 0.01 for pairs quoted in yen, 0.0001 for the rest. */
  pip_size: number;
  last_week_pips: number;
  this_week_pips: number | null;
  normal_week_pips: number;
  range_low: number;
  range_high: number;
  range_held: number;
  trend_13w: TrendDirection;
  trend_52w: TrendDirection;
}

/** This week's picture for every pair (forex_weekly.overview). */
export interface ForexOverview {
  pairs: ForexPair[];
  as_of: string | null;
  history_from: string | null;
}

/** How a set of past weeks went for a plan (forex_weekly._summary). */
export interface ForexSummary {
  weeks: number;
  win_rate: number | null;
  win_rate_low: number | null;
  win_rate_high: number | null;
  wins: number;
  breakeven_win_rate: number | null;
  target_first: number;
  stop_first: number;
  week_ended: number;
  week_ended_avg_pct: number | null;
  avg_result_pct: number;
  week_ended_avg_pips: number | null;
  avg_result_pips: number;
}

/** The position that risks the chosen share of the account at the stop. */
export interface ForexSizing {
  position_usd: number;
  units: number;
  lots: number;
  leverage: number;
  /** What one pip is worth at this position size. */
  pip_value_usd: number;
  /** What one pip is worth on one standard lot (100,000 units). */
  pip_value_per_lot_usd: number;
  loss_at_stop_usd: number;
  gain_at_target_usd: number;
}

/** What a plan of the trader's own shape has done, and how big to trade it (forex_weekly.plan). */
export interface ForexPlan {
  pair: string;
  direction: Side;
  price: number;
  as_of: string;
  pip_size: number;
  stop_pips: number;
  target_pips: number;
  spread_pips: number;
  normal_week_pct: number;
  normal_week_pips: number;
  stop_pct: number;
  target_pct: number;
  stop_price: number;
  target_price: number;
  stop_in_weeks: number;
  target_in_weeks: number;
  all: ForexSummary;
  with_trend: ForexSummary;
  against_trend: ForexSummary;
  trend_13w: TrendDirection;
  plan_is_with_trend: boolean;
  sizing: ForexSizing;
  cost_pct: number;
  history_from: string;
  avg_result_usd: number;
}

export interface MoodPoint {
  date: string;
  value: number;
}

export interface AdvancedExits {
  stop: number | null;
  targets: Targets;
  take_profit: TakeProfit;
  with: ExitRecord | null;
  holding: ExitRecord | null;
  by_call: Record<string, ExitRecord>;
}

export interface AdvancedSnapshot {
  now: string;
  movers: Partial<Record<AssetClass, MoverGroup>>;
  breadth: Partial<Record<AssetClass, BreadthCount>>;
  worksheet: WorksheetRow[];
  /** Per market label: thirty days rebased to 100, one point every twelve hours. */
  performance: Record<string, number[]>;
  /** Where each thirty-day line starts (its 100): the market and its price then. */
  performance_start: Record<string, { symbol: string; price: number }>;
  correlation: Correlation;
  signals: SignalRow[];
  leverage: LeverageTable;
  risk: Risk;
  scoreboard: Scoreboard | null;
  funding: FundingRow[];
  forex: ForexPair[];
  mood_history: MoodPoint[];
  exits: AdvancedExits | null;
  /** Every market the movers and breadth are counted over. */
  universe: UniverseMarket[];
  /** How many markets the catalogue lists. */
  markets: number;
  /** Worksheet markets whose price history is still downloading (not in the Python version). */
  pending: number;
}
