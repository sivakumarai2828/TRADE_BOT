"""Supabase CRUD for user-logged manual positions (stocks + options on Robinhood)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
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
# Stop loss monitor — called every 5 min by scheduler
# ---------------------------------------------------------------------------

def check_stop_losses(alpaca_api_key: str, alpaca_secret_key: str) -> None:
    """Fetch live prices for open stock positions and alert if stop breached."""
    positions = get_open_positions()
    if not positions:
        return

    stock_positions = [p for p in positions if p.get("asset_type") == "stock" and p.get("stop_price")]
    option_positions = [p for p in positions if p.get("asset_type") == "option" and p.get("underlying_stop")]
    monitored = stock_positions + option_positions
    if not monitored:
        return

    symbols = list({p["symbol"] for p in monitored})
    prices = _fetch_prices(symbols, alpaca_api_key, alpaca_secret_key)

    from telegram_notify import notify_user_stop_loss
    for pos in monitored:
        sym = pos["symbol"]
        price = prices.get(sym)
        if price is None:
            continue

        if pos.get("asset_type") == "stock":
            stop = pos["stop_price"]
            side = pos.get("side", "BUY")
            breached = (side == "BUY" and price <= stop) or (side == "SELL" and price >= stop)
            if breached:
                notify_user_stop_loss(
                    symbol=sym,
                    asset_type="stock",
                    current_price=price,
                    stop_price=stop,
                    entry_price=pos.get("entry_price", 0),
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
