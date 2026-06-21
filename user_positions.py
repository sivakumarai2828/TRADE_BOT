"""Supabase CRUD for user-logged manual positions (stocks + options on Robinhood)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional


def _client():
    from persistence import _get_client
    return _get_client()


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def save_user_position(
    symbol: str,
    side: str,          # "BUY" / "SELL"
    asset_type: str,    # "stock" / "option"
    qty: float,
    entry_price: float,
    stop_price: Optional[float] = None,
    target_price: Optional[float] = None,
    notes: str = "",
    # options-specific
    option_type: Optional[str] = None,    # "call" / "put"
    strike: Optional[float] = None,
    expiry: Optional[str] = None,         # "2026-05-02"
    underlying_stop: Optional[float] = None,  # underlying price that triggers exit alert
) -> dict:
    """Insert a new user-logged position. Returns the saved row."""
    c = _client()
    if not c:
        return {}
    row = {
        "symbol": symbol.upper(),
        "side": side.upper(),
        "asset_type": asset_type,
        "qty": qty,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "notes": notes,
        "option_type": option_type,
        "strike": strike,
        "expiry": expiry,
        "underlying_stop": underlying_stop,
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        res = c.table("user_positions").insert(row).execute()
        return res.data[0] if res.data else row
    except Exception as exc:
        logging.warning("user_positions save failed: %s", exc)
        return row


def close_user_position(position_id: int, exit_price: float, reason: str = "manual") -> bool:
    """Mark a position as closed with exit price."""
    c = _client()
    if not c:
        return False
    try:
        c.table("user_positions").update({
            "status": "closed",
            "exit_price": exit_price,
            "exit_reason": reason,
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", position_id).execute()
        return True
    except Exception as exc:
        logging.warning("user_positions close failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_open_positions() -> list[dict]:
    """All open user positions."""
    c = _client()
    if not c:
        return []
    try:
        res = c.table("user_positions").select("*").eq("status", "open").execute()
        return res.data or []
    except Exception as exc:
        logging.warning("user_positions fetch failed: %s", exc)
        return []


def get_all_positions(limit: int = 50) -> list[dict]:
    """Recent positions (open + closed)."""
    c = _client()
    if not c:
        return []
    try:
        res = (
            c.table("user_positions")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logging.warning("user_positions fetch_all failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Live enrichment — current price + unrealized PnL for the dashboard
# ---------------------------------------------------------------------------

def _parse_dt(value) -> Optional[datetime]:
    """Parse an ISO timestamp from Supabase into an aware datetime (UTC)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def enrich_open_positions(api_key: str = "", secret_key: str = "") -> list[dict]:
    """Open positions decorated with live price + unrealized PnL + progress.

    Each row gains: current_price, unrealized_pnl, unrealized_pnl_pct,
    days_held, pct_to_target, pct_to_stop. Falls back gracefully when a
    price can't be fetched (current_price stays None).
    """
    positions = get_open_positions()
    if not positions:
        return []

    india_syms = list({p["symbol"] for p in positions if p["symbol"].endswith(".NS")})
    us_syms = list({p["symbol"] for p in positions if not p["symbol"].endswith(".NS")})

    prices: dict[str, float] = {}
    if us_syms and api_key:
        prices.update(_fetch_prices(us_syms, api_key, secret_key))
    if india_syms:
        prices.update(_fetch_india_prices(india_syms))

    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for pos in positions:
        row = dict(pos)
        sym = pos["symbol"]
        price = prices.get(sym)
        entry = float(pos.get("entry_price") or 0)
        qty = float(pos.get("qty") or 0)
        side = (pos.get("side") or "BUY").upper()
        stop = pos.get("stop_price")
        target = pos.get("target_price")

        row["current_price"] = price
        if price is not None and entry > 0:
            direction = 1 if side == "BUY" else -1
            row["unrealized_pnl"] = round((price - entry) * qty * direction, 2)
            row["unrealized_pnl_pct"] = round((price - entry) / entry * 100 * direction, 2)
            # progress 0–100% from entry toward target / stop
            if target and target != entry:
                row["pct_to_target"] = max(0, min(100, round(
                    (price - entry) / (target - entry) * 100, 1)))
            if stop and stop != entry:
                row["pct_to_stop"] = max(0, min(100, round(
                    (entry - price) / (entry - stop) * 100, 1)))
        else:
            row["unrealized_pnl"] = None
            row["unrealized_pnl_pct"] = None

        created = _parse_dt(pos.get("created_at"))
        row["days_held"] = (now - created).days if created else None
        out.append(row)
    return out


def weekly_summary(api_key: str = "", secret_key: str = "") -> dict:
    """Realized PnL since Monday + live unrealized PnL of open positions."""
    now = datetime.now(timezone.utc)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)

    c = _client()
    realized = 0.0
    closed_count = 0
    wins = 0
    if c:
        try:
            res = (c.table("user_positions")
                   .select("*")
                   .eq("status", "closed")
                   .gte("closed_at", monday.isoformat())
                   .execute())
            for r in res.data or []:
                entry = float(r.get("entry_price") or 0)
                exit_p = float(r.get("exit_price") or 0)
                qty = float(r.get("qty") or 0)
                side = (r.get("side") or "BUY").upper()
                direction = 1 if side == "BUY" else -1
                pnl = (exit_p - entry) * qty * direction
                realized += pnl
                closed_count += 1
                if pnl > 0:
                    wins += 1
        except Exception as exc:
            logging.warning("weekly_summary realized fetch failed: %s", exc)

    open_live = enrich_open_positions(api_key, secret_key)
    unrealized = sum(p.get("unrealized_pnl") or 0 for p in open_live)

    return {
        "week_start": monday.date().isoformat(),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(realized + unrealized, 2),
        "closed_trades": closed_count,
        "wins": wins,
        "open_count": len(open_live),
    }


# ---------------------------------------------------------------------------
# Stop loss monitor — called every 5 min by scheduler
# ---------------------------------------------------------------------------

def check_stop_losses(alpaca_api_key: str, alpaca_secret_key: str) -> None:
    """Fetch live prices for open positions and alert on stop breach or target hit."""
    positions = get_open_positions()
    if not positions:
        return

    stock_positions = [p for p in positions if p.get("asset_type") == "stock"
                       and (p.get("stop_price") or p.get("target_price"))]
    option_positions = [p for p in positions if p.get("asset_type") == "option" and p.get("underlying_stop")]
    monitored = stock_positions + option_positions
    if not monitored:
        return

    # Split US vs India symbols (India ends in .NS)
    india_syms = list({p["symbol"] for p in monitored if p["symbol"].endswith(".NS")})
    us_syms = list({p["symbol"] for p in monitored if not p["symbol"].endswith(".NS")})

    prices: dict[str, float] = {}
    if us_syms:
        prices.update(_fetch_prices(us_syms, alpaca_api_key, alpaca_secret_key))
    if india_syms:
        prices.update(_fetch_india_prices(india_syms))

    from telegram_notify import notify_user_stop_loss, notify_user_target_hit
    for pos in monitored:
        sym = pos["symbol"]
        price = prices.get(sym)
        if price is None:
            continue
        market = "IN" if sym.endswith(".NS") else "US"

        if pos.get("asset_type") == "stock":
            stop = pos.get("stop_price")
            target = pos.get("target_price")
            side = pos.get("side", "BUY")

            # Stop loss check
            if stop:
                breached = (side == "BUY" and price <= stop) or (side == "SELL" and price >= stop)
                if breached:
                    notify_user_stop_loss(
                        symbol=sym,
                        asset_type="stock",
                        current_price=price,
                        stop_price=stop,
                        entry_price=pos.get("entry_price", 0),
                        market=market,
                    )

            # Target hit check
            if target:
                hit = (side == "BUY" and price >= target) or (side == "SELL" and price <= target)
                if hit:
                    notify_user_target_hit(
                        symbol=sym,
                        asset_type="stock",
                        current_price=price,
                        target_price=target,
                        entry_price=pos.get("entry_price", 0),
                        market=market,
                    )

        elif pos.get("asset_type") == "option":
            underlying_stop = pos["underlying_stop"]
            option_type = pos.get("option_type", "call")
            breached = (option_type == "call" and price <= underlying_stop) or \
                       (option_type == "put" and price >= underlying_stop)
            if breached:
                notify_user_stop_loss(
                    symbol=sym,
                    asset_type="option",
                    current_price=price,
                    stop_price=underlying_stop,
                    entry_price=pos.get("entry_price", 0),
                    option_detail=f"{pos.get('option_type','').upper()} ${pos.get('strike')} exp {pos.get('expiry')}",
                )


def _fetch_prices(symbols: list[str], api_key: str, secret_key: str) -> dict[str, float]:
    """Fetch latest prices from Alpaca data API (free, no trading needed)."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest
        client = StockHistoricalDataClient(api_key, secret_key)
        req = StockLatestTradeRequest(symbol_or_symbols=symbols)
        trades = client.get_stock_latest_trade(req)
        return {sym: float(trade.price) for sym, trade in trades.items()}
    except Exception as exc:
        logging.warning("user_positions price fetch failed: %s", exc)
        return {}


def _fetch_india_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch latest prices for NSE stocks via yfinance (15-min delayed)."""
    result: dict[str, float] = {}
    try:
        import yfinance as yf
        for sym in symbols:
            try:
                ticker = yf.Ticker(sym)
                price = ticker.fast_info.last_price
                if price and price > 0:
                    result[sym] = float(price)
            except Exception as exc:
                logging.warning("India price fetch failed for %s: %s", sym, exc)
    except ImportError:
        logging.warning("yfinance not installed — cannot fetch India prices")
    return result
