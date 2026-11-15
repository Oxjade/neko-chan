// Number handling that has to agree with the Python reference to the last
// digit: how NumPy adds up an array, and how Python rounds a number into text.

type Numbers = ArrayLike<number>;

/** Sum of values[from..to) in the order NumPy adds them (pairwise, in blocks of eight). */
export function pairwiseSum(values: Numbers, from = 0, to = values.length): number {
  const n = to - from;
  if (n < 8) {
    let total = 0;
    for (let i = from; i < to; i += 1) total += values[i];
    return total;
  }
  if (n <= 128) {
    const r = [values[from], values[from + 1], values[from + 2], values[from + 3],
               values[from + 4], values[from + 5], values[from + 6], values[from + 7]];
    const whole = n - (n % 8);
    let i = 8;
    for (; i < whole; i += 8) {
      for (let k = 0; k < 8; k += 1) r[k] += values[from + i + k];
    }
    let total = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
    for (; i < n; i += 1) total += values[from + i];
    return total;
  }
  let half = Math.floor(n / 2);
  half -= half % 8;
  return pairwiseSum(values, from, from + half) + pairwiseSum(values, from + half, to);
}

export function mean(values: Numbers, from = 0, to = values.length): number {
  return pairwiseSum(values, from, to) / (to - from);
}

/** Sample standard deviation (divides by n - 1), as numpy.std(ddof=1). */
export function sampleStd(values: Numbers, from = 0, to = values.length): number {
  const n = to - from;
  const centre = mean(values, from, to);
  const squares = new Float64Array(n);
  for (let i = 0; i < n; i += 1) {
    const d = values[from + i] - centre;
    squares[i] = d * d;
  }
  return Math.sqrt(pairwiseSum(squares) / (n - 1));
}

/** Differences of natural logs: the move from each value to the next. */
export function logReturns(values: Numbers): Float64Array {
  const out = new Float64Array(Math.max(0, values.length - 1));
  let prev = values.length ? Math.log(values[0]) : 0;
  for (let i = 1; i < values.length; i += 1) {
    const next = Math.log(values[i]);
    out[i - 1] = next - prev;
    prev = next;
  }
  return out;
}

/**
 * sum(values) as Python 3.12 and later add floats: a running total with a
 * correction term for the rounding lost at each step (Neumaier's method).
 */
export function pySum(values: Iterable<number>): number {
  let started = false;
  let total = 0;
  let lost = 0;
  for (const x of values) {
    if (!started) {
      total = 0 + x;
      started = true;
      continue;
    }
    const next = total + x;
    lost += Math.abs(total) >= Math.abs(x) ? (total - next) + x : (x - next) + total;
    total = next;
  }
  if (lost && Number.isFinite(lost)) total += lost;
  return total;
}

// ---- Python's number formatting ------------------------------------------------

function isNegative(v: number): boolean {
  return v < 0 || Object.is(v, -0);
}

/**
 * format(v, ".{digits}f") as Python does it: an exact tie goes to the even
 * neighbour (2.5 becomes "2", where JavaScript's toFixed gives "3").
 */
export function pyFixed(v: number, digits: number): string {
  if (!Number.isFinite(v)) return Number.isNaN(v) ? "nan" : v < 0 ? "-inf" : "inf";
  const abs = Math.abs(v);
  let text = abs.toFixed(digits);
  // toFixed(100) writes the exact binary value; a tie shows as "5" followed only by zeros.
  if (abs < 1e21) {
    const exact = abs.toFixed(100);
    const cut = exact.indexOf(".") + 1 + digits;
    const rest = exact.slice(cut);
    if (rest[0] === "5" && /^50*$/.test(rest)) {
      const kept = exact.slice(0, cut).replace(/\.$/, "");
      const lastDigit = Number(kept[kept.length - 1]);
      if (lastDigit % 2 === 0) text = kept;         // already even: round down, not up
    }
  }
  return (isNegative(v) ? "-" : "") + text;
}

/** format(v, ",.{digits}f"): fixed decimals with thousands separators. */
export function pyGrouped(v: number, digits: number): string {
  const text = pyFixed(Math.abs(v), digits);
  const [whole, fraction] = text.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return (isNegative(v) ? "-" : "") + grouped + (fraction === undefined ? "" : `.${fraction}`);
}
