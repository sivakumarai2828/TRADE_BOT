"""Trading universes.

These are STARTING universes, not filters. Each cycle the AI may also request
up to 5 extra symbols it wants data for (e.g. something it read about in its
own journal or a sector it wants to rotate into). data.fetch_snapshot() will
serve any valid ticker.

Sized deliberately large (2026-08-25): a 39-name universe run through a real
quality filter yielded 0-1 buyable candidates on a typical day, which made the
capital-deployment target unreachable. More names = more chances to fill the
book WITHOUT lowering entry standards.
"""

US_UNIVERSE = [
    # Mega tech / semis / hardware
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO",
    "AMD", "QCOM", "MU", "ASML", "TSM", "SMCI", "INTC", "TXN", "ADI",
    "LRCX", "KLAC", "AMAT", "MRVL", "ANET", "CSCO", "IBM", "NXPI",
    # Software / internet
    "CRM", "NOW", "PLTR", "SHOP", "NFLX", "UBER", "ORCL", "ADBE", "INTU",
    "PANW", "CRWD", "SNOW", "DDOG", "NET", "MDB", "TEAM", "WDAY", "FTNT",
    "ACN", "ABNB", "BKNG", "SPOT", "APP",
    # Financials
    "JPM", "GS", "V", "MA", "BAC", "MS", "WFC", "C", "SCHW", "BLK",
    "SPGI", "AXP", "PYPL", "COF", "PNC", "USB", "CB", "PGR", "MMC",
    "ICE", "CME", "AON",
    # Healthcare
    "LLY", "UNH", "JNJ", "ABBV", "MRK", "PFE", "TMO", "DHR", "AMGN",
    "GILD", "VRTX", "REGN", "ISRG", "SYK", "BSX", "MDT", "CI", "CVS",
    "ELV", "HCA", "ZTS",
    # Industrials
    "CAT", "GE", "HON", "UNP", "UPS", "RTX", "LMT", "NOC", "GD", "BA",
    "DE", "EMR", "ETN", "PH", "ITW", "MMM", "CSX", "NSC", "WM", "FDX",
    # Energy / materials
    "XOM", "CVX", "COP", "SLB", "EOG", "PSX", "MPC", "VLO", "OXY",
    "LIN", "APD", "SHW", "FCX", "NEM", "NUE",
    # Consumer
    "COST", "WMT", "HD", "MCD", "NKE", "SBUX", "TGT", "LOW", "TJX",
    "ROST", "LULU", "CMG", "YUM", "DIS", "MAR", "F", "GM",
    "PG", "KO", "PEP", "PM", "MO", "MDLZ", "CL", "KMB", "GIS",
    # Telecom / utilities / REITs
    "T", "VZ", "TMUS", "NEE", "DUK", "SO", "AEP", "PLD", "AMT", "EQIX",
]

US_BENCHMARKS = ["SPY", "QQQ", "^VIX"]

INDIA_UNIVERSE = [
    # Large-cap core
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
    "HINDUNILVR.NS", "SBIN.NS", "BHARTIARTL.NS", "ITC.NS", "KOTAKBANK.NS",
    "LT.NS", "AXISBANK.NS", "BAJFINANCE.NS", "ASIANPAINT.NS", "MARUTI.NS",
    "HCLTECH.NS", "SUNPHARMA.NS", "TITAN.NS", "WIPRO.NS", "ULTRACEMCO.NS",
    "ADANIENT.NS", "ADANIPORTS.NS", "POWERGRID.NS", "NTPC.NS", "ONGC.NS",
    "JSWSTEEL.NS", "TATACONSUM.NS", "TATASTEEL.NS", "TECHM.NS", "BPCL.NS",
    "BAJAJFINSV.NS", "CIPLA.NS", "DRREDDY.NS", "EICHERMOT.NS", "GRASIM.NS",
    "HEROMOTOCO.NS", "HINDALCO.NS", "M&M.NS", "NESTLEIND.NS",
    # Banks / financials
    "BANKBARODA.NS", "PNB.NS", "CANBK.NS", "INDUSINDBK.NS", "FEDERALBNK.NS",
    "IDFCFIRSTB.NS", "CHOLAFIN.NS", "SBILIFE.NS", "HDFCLIFE.NS",
    "ICICIGI.NS", "ICICIPRULI.NS", "MUTHOOTFIN.NS", "PFC.NS", "RECLTD.NS",
    # IT / tech
    "LTIM.NS", "PERSISTENT.NS", "MPHASIS.NS", "COFORGE.NS", "TATAELXSI.NS",
    # Auto / industrials
    "TVSMOTOR.NS", "BAJAJ-AUTO.NS", "ASHOKLEY.NS", "MOTHERSON.NS",
    "BOSCHLTD.NS", "SIEMENS.NS", "ABB.NS", "BEL.NS", "HAL.NS", "INDIGO.NS",
    # Pharma / healthcare
    "DIVISLAB.NS", "LUPIN.NS", "AUROPHARMA.NS", "TORNTPHARM.NS",
    "ZYDUSLIFE.NS", "ALKEM.NS", "APOLLOHOSP.NS", "MAXHEALTH.NS",
    # Consumer
    "BRITANNIA.NS", "DABUR.NS", "GODREJCP.NS", "MARICO.NS", "COLPAL.NS",
    "PAGEIND.NS", "TRENT.NS", "DMART.NS", "JUBLFOOD.NS", "HAVELLS.NS",
    "PIDILITE.NS", "BERGEPAINT.NS",
    # Materials / energy / power / infra
    "SHREECEM.NS", "AMBUJACEM.NS", "VEDL.NS", "JINDALSTEL.NS", "HINDZINC.NS",
    "NMDC.NS", "SAIL.NS", "GAIL.NS", "IOC.NS", "TATAPOWER.NS",
    "ADANIGREEN.NS", "ADANIPOWER.NS", "TORNTPOWER.NS", "SRF.NS", "UPL.NS",
    "DLF.NS", "INDHOTEL.NS",
]

INDIA_BENCHMARKS = ["^NSEI", "^NSEBANK"]


def universe_for(market: str) -> tuple[list[str], list[str]]:
    if market == "US":
        return US_UNIVERSE, US_BENCHMARKS
    if market == "INDIA":
        return INDIA_UNIVERSE, INDIA_BENCHMARKS
    raise ValueError(f"unknown market {market}")
