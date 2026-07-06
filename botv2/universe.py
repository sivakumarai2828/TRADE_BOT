"""Trading universes.

These are STARTING universes, not filters. Each cycle the AI may also request
up to 5 extra symbols it wants data for (e.g. something it read about in its
own journal or a sector it wants to rotate into). data.fetch_snapshot() will
serve any valid ticker.
"""

US_UNIVERSE = [
    # Mega tech
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO",
    # Semis / hardware
    "AMD", "QCOM", "MU", "ASML", "TSM", "SMCI",
    # Software / internet
    "CRM", "NOW", "PLTR", "SHOP", "NFLX", "UBER", "ORCL", "ADBE",
    # Finance
    "JPM", "GS", "V", "MA", "BAC",
    # Healthcare
    "LLY", "UNH", "JNJ", "ABBV",
    # Industrial / energy / consumer
    "CAT", "GE", "XOM", "CVX", "COST", "WMT", "HD", "MCD",
]

US_BENCHMARKS = ["SPY", "QQQ", "^VIX"]

INDIA_UNIVERSE = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
    "HINDUNILVR.NS", "SBIN.NS", "BHARTIARTL.NS", "ITC.NS", "KOTAKBANK.NS",
    "LT.NS", "AXISBANK.NS", "BAJFINANCE.NS", "ASIANPAINT.NS", "MARUTI.NS",
    "HCLTECH.NS", "SUNPHARMA.NS", "TITAN.NS", "WIPRO.NS", "ULTRACEMCO.NS",
    "ADANIENT.NS", "ADANIPORTS.NS", "POWERGRID.NS", "NTPC.NS", "ONGC.NS",
    "JSWSTEEL.NS", "TATACONSUM.NS", "TATASTEEL.NS", "TECHM.NS", "BPCL.NS",
    "BAJAJFINSV.NS", "CIPLA.NS", "DRREDDY.NS", "EICHERMOT.NS", "GRASIM.NS",
    "HEROMOTOCO.NS", "HINDALCO.NS", "M&M.NS", "NESTLEIND.NS", "TATAMOTORS.NS",
]

INDIA_BENCHMARKS = ["^NSEI", "^NSEBANK"]


def universe_for(market: str) -> tuple[list[str], list[str]]:
    if market == "US":
        return US_UNIVERSE, US_BENCHMARKS
    if market == "INDIA":
        return INDIA_UNIVERSE, INDIA_BENCHMARKS
    raise ValueError(f"unknown market {market}")
