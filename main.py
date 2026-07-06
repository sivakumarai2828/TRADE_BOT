"""TRADE_BOT_V2 entry point.

Schedule (market-LOCAL clocks, DST-safe via zoneinfo):
  INDIA (NSE, Asia/Kolkata):
    10:00 IST        AI decision cycle
    every 20 min 09:15–15:30 IST  stop/target monitor + mid-session kill switch
  US (NYSE, America/New_York):
    10:00 ET         AI decision cycle (correct in summer AND winter)
    every 20 min 09:30–16:00 ET   stop/target monitor + mid-session kill switch
  SATURDAY 08:00 UTC  weekly AI self-review (both markets)

CLI:
  python main.py            run the scheduler loop
  python main.py once us    run one US cycle now (testing)
  python main.py once india run one India cycle now
  python main.py review     run self-review now
  python main.py status     print portfolio state
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from botv2.ai_pm import PortfolioManagerAI
from botv2.alpaca_mirror import AlpacaMirror
from botv2.config import load_config
from botv2.executor import MarketRunner
from botv2.storage import make_journal


def build_runners(cfg):
    from botv2.ledger import PaperLedger

    journal = make_journal(cfg.db_path)
    ai = PortfolioManagerAI(cfg.anthropic_api_key, cfg.anthropic_model,
                            cfg.ai_decision_max_tokens)
    runners = {}
    if cfg.trade_us:
        runners["US"] = MarketRunner(
            "US", "USD", cfg, journal, ai,
            PaperLedger(journal, "US", cfg.us_start_cash),
            alpaca=AlpacaMirror(cfg.alpaca_key, cfg.alpaca_secret, cfg.alpaca_base_url),
        )
    if cfg.trade_india:
        runners["INDIA"] = MarketRunner(
            "INDIA", "INR", cfg, journal, ai,
            PaperLedger(journal, "INDIA", cfg.india_start_cash),
        )
    return runners


NY = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")

MARKETS = {
    # market: (tz, decision (h,m), session open (h,m), session close (h,m))
    "INDIA": (IST, (10, 0), (9, 15), (15, 30)),
    "US": (NY, (10, 0), (9, 30), (16, 0)),
}


def main() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    log = logging.getLogger("botv2.main")
    runners = build_runners(cfg)

    args = sys.argv[1:]
    if args[:1] == ["once"]:
        market = (args[1] if len(args) > 1 else "us").upper()
        runners[market].run_cycle()
        return
    if args[:1] == ["review"]:
        for r in runners.values():
            r.weekly_review()
        return
    if args[:1] == ["status"]:
        for name, r in runners.items():
            print(f"\n[{name}] cash={r.ledger.cash:,.2f} equity={r.ledger.equity():,.2f}")
            for p in r.ledger.positions():
                print(f"  {p['symbol']}: {p['qty']:.0f} @ {p['entry']:.2f} "
                      f"now {p['last_price']:.2f} ({p['unrealized_pnl_pct']:+.1f}%) "
                      f"stop {p['stop']} target {p['target']}")
        return

    # ── scheduled mode (market-local clocks, DST-safe) ──────────
    last_fired: dict[str, str] = {}

    def fire_once(name: str, key: str, fn) -> None:
        if last_fired.get(name) != key:
            last_fired[name] = key
            try:
                fn()
            except Exception:
                log.exception("scheduled job %s failed", name)

    log.info("TRADE_BOT_V2 scheduler running (markets: %s)", ", ".join(runners))
    journal = make_journal(cfg.db_path)
    while True:
        journal.kv_set("heartbeat", str(time.time()))  # dashboard "bot online" signal
        now_utc = datetime.now(timezone.utc)
        for market, (tz, decide_at, open_at, close_at) in MARKETS.items():
            if market not in runners:
                continue
            local = now_utc.astimezone(tz)
            if local.weekday() >= 5:  # weekend in market-local time
                continue
            day = local.strftime("%Y-%m-%d")
            hm = (local.hour, local.minute)
            # daily AI decision at 10:00 local
            if hm == decide_at:
                fire_once(f"{market}_cycle", day, runners[market].run_cycle)
            # stop/target monitor every 20 min during the session
            if open_at <= hm <= close_at and local.minute % 20 == 0:
                fire_once(f"{market}_mon", f"{day}T{local.hour}:{local.minute}",
                          runners[market].monitor_positions)
        # weekly self-review: Saturday 08:00 UTC
        if now_utc.weekday() == 5 and (now_utc.hour, now_utc.minute) == (8, 0):
            for r in runners.values():
                fire_once("review", now_utc.strftime("%Y-%m-%d"), r.weekly_review)
        time.sleep(20)


if __name__ == "__main__":
    main()
