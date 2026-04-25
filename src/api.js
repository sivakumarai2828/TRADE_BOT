/**
 * Frontend API client.
 *
 * All calls go to the Flask backend (default http://localhost:5000).
 * Override the base URL with the VITE_API_URL environment variable:
 *   VITE_API_URL=http://192.168.1.10:5000  npm run dev
 */

const BASE = import.meta.env.VITE_API_URL ?? "";

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.message ?? `HTTP ${res.status}`);
  }
  return res.json();
}

/** Lightweight server liveness check. */
export const fetchHealth = () => request("/health");

/** Full bot state — metrics, signal, position, logs, settings. */
export const fetchStatus = () => request("/status");

/** Current AI signal only. */
export const fetchSignal = () => request("/signals");

/** Open position (null when flat). */
export const fetchPositions = () => request("/positions");

/** Last 30 activity log entries. */
export const fetchLogs = () => request("/logs");

/** Recent OHLCV candles for the chart. */
export const fetchCandles = (symbol = "BTC/USDT") =>
  request(`/candles?symbol=${encodeURIComponent(symbol)}`);

/**
 * Start the bot. Optionally pass settings to override .env values.
 * @param {object} [settings]
 */
export const startBot = (settings = {}) =>
  request("/start", { method: "POST", body: JSON.stringify(settings) });

/** Stop the bot gracefully. */
export const stopBot = () => request("/stop", { method: "POST" });

/** Force-close a position. Pass symbol to close one pair, omit to close all. */
export const closePosition = (symbol) =>
  request("/close", { method: "POST", body: JSON.stringify(symbol ? { symbol } : {}) });

/**
 * Push updated runtime settings to the backend.
 * @param {object} settings
 */
export const updateSettings = (settings) =>
  request("/settings", { method: "POST", body: JSON.stringify(settings) });

// ---------------------------------------------------------------------------
// Day Bot API
// ---------------------------------------------------------------------------

/** Full day bot state — metrics, positions, signals, watchlist, logs. */
export const fetchDayStatus = () => request("/daybot/status");

/** Start the day bot. */
export const startDayBot = () => request("/daybot/start", { method: "POST" });

/** Stop the day bot. */
export const stopDayBot = () => request("/daybot/stop", { method: "POST" });

/** Day bot open positions. */
export const fetchDayPositions = () => request("/daybot/positions");

/** Day bot signals (per symbol). */
export const fetchDaySignals = () => request("/daybot/signals");

/** Day bot watchlist. */
export const fetchDayWatchlist = () => request("/daybot/watchlist");

/** Day bot activity logs. */
export const fetchDayLogs = () => request("/daybot/logs");

/** Update day bot settings (trade_mode, position_size_pct, shield thresholds). */
export const updateDaySettings = (settings) =>
  request("/daybot/settings", { method: "POST", body: JSON.stringify(settings) });

/** AI stock suggestions from evening analysis (entry zone, SL, target). */
export const fetchSuggestions = () => request("/daybot/suggestions");

/** AI options picks from 9:15 AM morning run. */
export const fetchOptionsSuggestions = () => request("/daybot/options-suggestions");

/** Open user-logged manual positions (Robinhood stocks + options). */
export const fetchUserPositions = () => request("/daybot/user-positions");

/**
 * Log a new user manual position.
 * @param {object} position - {symbol, side, asset_type, qty, entry_price, stop_price?, target_price?, notes?, option_type?, strike?, expiry?, underlying_stop?}
 */
export const addUserPosition = (position) =>
  request("/daybot/user-positions", { method: "POST", body: JSON.stringify(position) });

/**
 * Close a user position.
 * @param {number} id - position ID
 * @param {number} exitPrice
 * @param {string} [reason]
 */
export const closeUserPosition = (id, exitPrice, reason = "manual") =>
  request(`/daybot/user-positions/${id}/close`, {
    method: "POST",
    body: JSON.stringify({ exit_price: exitPrice, reason }),
  });
