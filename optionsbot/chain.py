"""Options chain fetcher — picks best strike/expiry within budget."""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional


OPTIONABLE = {"SPY", "QQQ"}  # most liquid, tightest spreads


def pick_contract(
    underlying: str,
    direction: str,        # "BUY" → call, "SELL" → put
    budget: float = 150.0, # max premium cost per contract (premium × 100)
) -> Optional[dict]:
    """Return best contract within budget or None."""
    if underlying not in OPTIONABLE:
        logging.warning("chain: %s not in optionable universe", underlying)
        return None

    option_type = "call" if direction == "BUY" else "put"
    rows = _fetch_chain(underlying, option_type)
    if not rows:
        return None

    # Filter: affordable + liquid
    affordable = [r for r in rows if r["mid"] > 0 and r["mid"] * 100 <= budget]
    liquid = [r for r in affordable if r["open_interest"] >= 200 and r["spread_pct"] < 0.20]
    candidates = liquid if liquid else affordable
    if not candidates:
        logging.warning("chain: no %s %s contracts within $%.0f budget", underlying, option_type, budget)
        return None

    # Pick closest to ATM
    best = min(candidates, key=lambda r: r["dist"])
    contract_symbol = _opra_symbol(underlying, best["strike"], best["expiry"], option_type)

    return {
        "underlying": underlying,
        "contract_symbol": contract_symbol,
        "option_type": option_type,
        "strike": best["strike"],
        "expiry": best["expiry"],
        "premium": best["mid"],
        "cost": round(best["mid"] * 100, 2),
        "open_interest": best["open_interest"],
        "iv": best.get("iv", 0.0),
        "spread_pct": best["spread_pct"],
    }


def _fetch_chain(underlying: str, option_type: str) -> list[dict]:
    try:
        import yfinance as yf

        ticker = yf.Ticker(underlying)
        exps = ticker.options
        if not exps:
            return []

        # Pick expiry 4-10 DTE (weekly)
        today = datetime.now(timezone.utc).date()
        target = None
        for exp in exps:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            if 4 <= dte <= 10:
                target = exp
                break
        if not target:
            target = exps[0]

        chain = ticker.option_chain(target)
        hist = ticker.history(period="1d")
        if hist.empty:
            return []

        current_price = float(hist["Close"].iloc[-1])
        df = chain.calls if option_type == "call" else chain.puts
        df = df.copy()
        df["dist"] = abs(df["strike"] - current_price)
        nearest = df.nsmallest(5, "dist")

        rows = []
        for _, row in nearest.iterrows():
            bid = float(row.get("bid", 0) or 0)
            ask = float(row.get("ask", 0) or 0)
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
            spread_pct = (ask - bid) / mid if mid > 0 else 1.0
            rows.append({
                "strike": float(row["strike"]),
                "expiry": target,
                "bid": round(bid, 2),
                "ask": round(ask, 2),
                "mid": round(mid, 2),
                "spread_pct": round(spread_pct, 3),
                "open_interest": int(row.get("openInterest", 0) or 0),
                "iv": round(float(row.get("impliedVolatility", 0) or 0), 3),
                "dist": float(row["dist"]),
            })
        return rows
    except Exception as exc:
        logging.warning("chain: fetch failed for %s: %s", underlying, exc)
        return []


def _opra_symbol(underlying: str, strike: float, expiry: str, option_type: str) -> str:
    """Build OPRA-format contract symbol: SPY260523C00520000"""
    exp_date = datetime.strptime(expiry, "%Y-%m-%d")
    exp_str = exp_date.strftime("%y%m%d")
    type_char = "C" if option_type == "call" else "P"
    strike_int = int(round(strike * 1000))
    return f"{underlying}{exp_str}{type_char}{strike_int:08d}"


def get_current_price(contract_symbol: str, api_key: str, secret_key: str) -> Optional[float]:
    """Fetch mid price of an open options contract via Alpaca."""
    try:
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.requests import OptionLatestQuoteRequest

        client = OptionHistoricalDataClient(api_key, secret_key)
        req = OptionLatestQuoteRequest(symbol_or_symbols=contract_symbol)
        quotes = client.get_option_latest_quote(req)
        quote = quotes.get(contract_symbol)
        if quote:
            bid = float(quote.bid_price or 0)
            ask = float(quote.ask_price or 0)
            if bid > 0 and ask > 0:
                return round((bid + ask) / 2, 2)
    except Exception as exc:
        logging.warning("chain: price fetch failed %s: %s", contract_symbol, exc)
    return None
