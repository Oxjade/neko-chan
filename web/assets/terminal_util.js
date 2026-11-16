// Small helpers shared by the terminal's basic and advanced screens.

export const COLORS = { up: "#22e07a", down: "#ff4d5e", amber: "#ffa028", cyan: "#35d6e6", grid: "#12262c",
                        text: "#8296a3", ink: "#d9e4ea", bg: "#0a1016", violet: "#b48cff" };
export const SERIES = ["#ffa028", "#35d6e6", "#b48cff", "#22e07a", "#ff7ab6", "#d9e4ea", "#ffe45c", "#ff4d5e"];

export function px(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const digits = v >= 1000 ? 1 : v >= 10 ? 2 : 4;
  return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function pct(v, digits = 2) {
  return v === null || v === undefined ? "–" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

export function usd(v) {
  if (v === null || v === undefined) return "–";
  return `${v < 0 ? "-" : "+"}$${Math.abs(v).toFixed(2)}`;
}

export function compact(v) {
  if (v === null || v === undefined) return "–";
  const abs = Math.abs(v);
  if (abs >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${(v / 1e3).toFixed(0)}K`;
  return v.toFixed(0);
}

export function tone(v) {
  return v > 0 ? "up" : v < 0 ? "down" : "";
}

// Background for a heat-map cell: green above zero, red below, stronger further out.
export function heat(value, full) {
  if (value === null || value === undefined || !full) return "";
  const strength = Math.min(1, Math.abs(value) / full) * 0.5;
  const rgb = value >= 0 ? "34,224,122" : "255,77,94";
  return `background:rgba(${rgb},${strength.toFixed(3)})`;
}

// A brief green or red wash behind a figure that moved. It is run as an animation of its
// own, which costs no layout, and not at all where the reader has asked for less motion.
const calm = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
let washes = null;
const lastFlash = new WeakMap();
const FLASH_MS = 350;

function tint(hex, fallback) {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex.trim());
  const rgb = m ? m.slice(1).map((part) => parseInt(part, 16)).join(", ") : fallback;
  return `rgba(${rgb}, 0.33)`;
}

function flash(node, up) {
  if (calm || typeof node.animate !== "function") return;
  if (!washes) {
    // the theme's own green and red, at a third of their strength
    const css = getComputedStyle(document.body);
    washes = [tint(css.getPropertyValue("--gain"), "34, 224, 122"), tint(css.getPropertyValue("--loss"), "255, 77, 94")];
  }
  // Held, then gone: a fade would repaint the figure on every frame, and with dozens of
  // figures moving each second that alone kept the page busy.
  const now = performance.now();
  if (now - (lastFlash.get(node) || 0) < FLASH_MS) return;
  lastFlash.set(node, now);
  node.animate([{ backgroundColor: up ? washes[0] : washes[1] }, { backgroundColor: "transparent" }],
               { duration: FLASH_MS, easing: "steps(1, end)" });
}

// Put a new figure on the page and, if it moved, flash it green or red for a moment.
// Returns whether the text changed.
export function cue(node, text, value) {
  if (!node) return false;
  const known = node.dataset.value !== undefined;
  if (node.textContent === text) {
    if (!known) node.dataset.value = String(value);
    return false;
  }
  const was = Number(node.dataset.value);
  node.textContent = text;
  node.dataset.value = String(value);
  if (known && node.dataset.flash !== "off" && value !== was) flash(node, value > was);
  return true;
}
