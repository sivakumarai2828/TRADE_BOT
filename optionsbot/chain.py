"""Options chain fetcher — picks best strike/expiry within budget.

Strategy: slightly OTM (delta ~0.35-0.45), 7-14 DTE.
Rationale: OTM gives leverage for 3x moves; 7-14 DTE avoids theta crush
           while leaving enough time for 2-3 day swing to play out.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional


# Individual high-beta stocks for momentum swings + liquid index ETFs as fallback
OPTIONABLE = {
    "NVDA",   # high beta, liquid options, huge momentum moves
    "AMD",    # high beta, affordable premiums
    "TSLA",   # high beta, big intraday/multiday swings
    "META",   # tech momentum, liquid options
    "AAPL",   # reliable liquidity, steady options chain
    "MSFT",   # large cap tech, liquid options chain
    "SPY",    # index fallback — always liquid
    "QQQ",    # index fallback — tech-heavy
}

# Minimum open interest thresholds per underlying type
_OI_MIN = {
    "SPY": 500, "QQQ": 500,                             # very liquid
    "NVDA": 100, "AMD": 100, "TSLA": 200,               # high activity
    "META": 100, "AAPL": 200, "MSFT": 200,              # large cap
}


def _fetch_rv(underlying: str) -> float:
    """Compute 30-day annualized realized volatility for underlying stock."""
    try:
        import yfinance as yf
        hist = yf.Ticker(underlying).history(period="35d")
        if hist.empty or len(hist) < 15:
            return 0.0
        returns = hist["Close"].pct_change().dropna()
        return float(returns.std() * (252 ** 0.5))
    except Exception as exc:
        logging.warning("chain: RV fetch failed for %s: %s", underlying, exc)
        return 0.0


def pick_contract(
    underlying: str,
    direction: str,               # "BUY" → call, "SELL" → put
    budget: float = 150.0,        # max premium cost per contract (premium × 100)
    target_otm_pct: float = None, # evening pre-analysis OTM% hint (overrides default range)
) -> Optional[dict]:
    """Return best OTM contract within budget, 7-14 DTE.

    target_otm_pct: if set by evening analysis, targets contracts near that OTM%.
                    Overrides default 4-12% range — uses ±3% window around target.
    IV/RV filter: skips when implied vol > 2.0× realized vol (options overpriced).
    """
    if underlying not in OPTIONABLE:
        logging.warning("chain: %s not in optionable universe", underlying)
        return None

    option_type = "call" if direction == "BUY" else "put"
    rows = _fetch_chain(underlying, option_type)
    if not rows:
        return None

    oi_min = _OI_MIN.get(underlying, 100)

    # Filter: affordable + liquid
    affordable = [r for r in rows if r["mid"] > 0 and r["mid"] * 100 <= budget]
    liquid = [r for r in affordable if r["open_interest"] >= oi_min and r["spread_pct"] < 0.25]
    candidates = liquid if liquid else affordable
    if not candidates:
        logging.warning("chain: no %s %s contracts within $%.0f budget", underlying, option_type, budget)
        return None

    # IV/RV filter — skip when implied vol is expensive vs realized vol.
    # Entering overpriced options = theta crushes premium even when direction is right.
    # Threshold 1.4× gives some buffer (options always carry some premium over RV).
    rv = _fetch_rv(underlying)
    if rv > 0:
        iv_list = [r["iv"] for r in candidates if r["iv"] > 0]
        if iv_list:
            avg_iv = sum(iv_list) / len(iv_list)
            iv_rv = avg_iv / rv
            logging.info(
                "chain: %s IV=%.1f%% RV=%.1f%% ratio=%.2f",
                underlying, avg_iv * 100, rv * 100, iv_rv,
            )
            if iv_rv > 1.5:
                # Threshold 1.5×: options always carry some premium over RV (risk premium).
                # Above 1.5× = overpriced — theta decay dominates, direction must be very strong
                # to overcome premium erosion. Reverted from 2.0× (too permissive).
                logging.warning(
                    "chain: %s IV/RV=%.2f > 1.5 — options overpriced, skip (IV=%.1f%% RV=%.1f%%)",
                    underlying, iv_rv, avg_iv * 100, rv * 100,
                )
                return None

    # OTM targeting:
    #   If evening analysis provided a target_otm_pct → use ±3% window around it
    #   Otherwise default 4-12% range (cheaper premiums vs old 2-6%)
    # For calls: OTM means strike > current_price  |  For puts: OTM means strike < current_price
    if target_otm_pct is not None:
        lo = max(1.0, target_otm_pct - 3.0)
        hi = target_otm_pct + 3.0
        otm = [r for r in candidates if lo <= r.get("otm_pct", 0) <= hi]
        if not otm:
            # Relax ±5% if nothing in tight window
            otm = [r for r in candidates if max(1.0, target_otm_pct - 5.0) <= r.get("otm_pct", 0) <= target_otm_pct + 5.0]
        logging.info("chain: evening OTM target=%.1f%% window=[%.1f-%.1f%%] → %d candidates", target_otm_pct, lo, hi, len(otm))
    else:
        otm = [r for r in candidates if r.get("otm_pct", 0) >= 4.0 and r.get("otm_pct", 0) <= 12.0]
        if not otm:
            # Fallback: any OTM contract 2-15%
            otm = [r for r in candidates if r.get("otm_pct", 0) >= 2.0 and r.get("otm_pct", 0) <= 15.0]
    pool = otm if otm else candidates  # last resort: any affordable

    # Delta targeting — prefer delta 0.15-0.35 (cheaper OTM leverage zone).
    # Only apply when chain provides delta data (not always available via yfinance).
    delta_pool = [r for r in pool if 0.10 <= r.get("delta", 0) <= 0.35]
    if len(delta_pool) >= 2:
        pool = delta_pool
        pool.sort(key=lambda r: abs(r.get("delta", 0.25) - 0.25))  # target delta 0.25
        logging.info("chain: delta filter → %d contracts, targeting delta ~0.25", len(pool))
    else:
        # Sort by proximity to target OTM% or default 7%
        _sort_target = target_otm_pct if target_otm_pct is not None else 7.0
        pool.sort(key=lambda r: abs(r.get("otm_pct", 0) - _sort_target))

    best = pool[0]

    contract_symbol = _opra_symbol(underlying, best["strike"], best["expiry"], option_type)

    logging.info(
        "chain: picked %s %s $%.0f exp=%s | OTM=%.1f%% delta=%.2f premium=$%.2f cost=$%.0f IV=%.0f%%",
        underlying, option_type.upper(), best["strike"], best["expiry"],
        best.get("otm_pct", 0), best.get("delta", 0), best["mid"], best["mid"] * 100, best.get("iv", 0) * 100,
    )

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
        "otm_pct": best.get("otm_pct", 0.0),
        "delta": best.get("delta", 0.0),
    }


def _fetch_chain(underlying: str, option_type: str) -> list[dict]:
    try:
        import yfinance as yf

        ticker = yf.Ticker(underlying)
        exps = ticker.options
        if not exps:
            return []

        # Pick expiry 7-14 DTE — sweet spot for 2-3 day swing trades
        # Enough time to avoid theta crush; not so much that premium is expensive
        today = datetime.now(timezone.utc).date()
        target = None
        for exp in exps:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            if 7 <= dte <= 14:
                target = exp
                break
        # Fallback: 4-21 DTE if no 7-14 found
        if not target:
            for exp in exps:
                exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                dte = (exp_date - today).days
                if 4 <= dte <= 21:
                    target = exp
                    break
        if not target:
            target = exps[0]

        chain = ticker.option_chain(target)
        hist = ticker.history(period="2d")
        if hist.empty:
            return []

        current_price = float(hist["Close"].iloc[-1])
        df = chain.calls if option_type == "call" else chain.puts
        df = df.copy()
        df["dist"] = abs(df["strike"] - current_price)

        # Look at wider range: nearest 40 strikes — captures deeper OTM (cheaper contracts)
        # 20 was too narrow: missed 5-12% OTM strikes where cheap premiums live
        nearest = df.nsmallest(40, "dist")

        def _safe_float(val, default=0.0) -> float:
            try:
                v = float(val or default)
                return v if v == v else default  # NaN check: NaN != NaN
            except (TypeError, ValueError):
                return default

        def _safe_int(val, default=0) -> int:
            try:
                v = float(val or default)
                return int(v) if v == v else default  # NaN check
            except (TypeError, ValueError):
                return default

        rows = []
        for _, row in nearest.iterrows():
            bid = _safe_float(row.get("bid", 0))
            ask = _safe_float(row.get("ask", 0))
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
            spread_pct = (ask - bid) / mid if mid > 0 else 1.0
            strike = _safe_float(row.get("strike", 0))
            if strike <= 0:
                continue

            # Calculate OTM %: positive = OTM, negative = ITM
            if option_type == "call":
                otm_pct = (strike - current_price) / current_price * 100
            else:
                otm_pct = (current_price - strike) / current_price * 100

            delta_raw = _safe_float(row.get("delta", 0))
            rows.append({
                "strike": strike,
                "expiry": target,
                "bid": round(bid, 2),
                "ask": round(ask, 2),
                "mid": round(mid, 2),
                "spread_pct": round(spread_pct, 3),
                "open_interest": _safe_int(row.get("openInterest", 0)),
                "iv": round(_safe_float(row.get("impliedVolatility", 0)), 3),
                "dist": _safe_float(row.get("dist", 0)),
                "otm_pct": round(otm_pct, 2),
                "delta": round(abs(delta_raw), 3),  # absolute delta 0→1
            })
        return rows
    except Exception as exc:
        logging.warning("chain: fetch failed for %s: %s", underlying, exc)
        return []


def _opra_symbol(underlying: str, strike: float, expiry: str, option_type: str) -> str:
    """Build OPRA-format contract symbol: NVDA260523C00120000"""
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
