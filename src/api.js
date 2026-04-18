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
