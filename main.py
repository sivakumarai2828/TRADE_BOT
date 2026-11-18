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
  python main.py pending    list entries waiting for approval
  python main.py approve <id> / reject <id>   decide one from the shell
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from botv2.ai_pm import PortfolioManagerAI
from botv2.alpaca_mirror import AlpacaMirror
from botv2.approval import ApprovalDesk
from botv2 import control, notify
from botv2.config import check_broker_config, load_config
from botv2.executor import MarketRunner
from botv2.skeptic import Skeptic
from botv2.storage import make_journal


def build_desk(cfg, journal):
    """Skeptic + ApprovalDesk from config. Approval mode 'telegram' without
    Telegram credentials refuses to start: silently trading unapproved would
    defeat the point, and silently never trading would go unnoticed."""
    skeptic = None
    if cfg.skeptic_enabled:
        backend = PortfolioManagerAI(cfg.anthropic_api_key,
                                     cfg.skeptic_model or cfg.anthropic_model,
                                     cfg.ai_decision_max_tokens, cfg.openrouter_api_key,
                                     cfg.ai_reasoning_effort)
        skeptic = Skeptic(backend)
    desk = None
    if cfg.approval_mode == "telegram":
        if not (cfg.telegram_token and cfg.telegram_chat_id):
            raise SystemExit("APPROVAL_MODE=telegram needs TELEGRAM_TOKEN and "
                             "TELEGRAM_CHAT_ID (or set APPROVAL_MODE=off)")
        desk = ApprovalDesk(journal, cfg.telegram_token, cfg.telegram_chat_id,
                            cfg.approval_timeout_min, cfg.approval_max_drift_pct)
    elif cfg.approval_mode != "off":
        raise SystemExit(f"APPROVAL_MODE must be telegram or off, got {cfg.approval_mode!r}")
    return skeptic, desk


def build_runners(cfg, journal=None):
    from botv2.ledger import PaperLedger

    journal = journal or make_journal(cfg.db_path)
    ai = PortfolioManagerAI(cfg.anthropic_api_key, cfg.anthropic_model,
                            cfg.ai_decision_max_tokens, cfg.openrouter_api_key,
                            cfg.ai_reasoning_effort)
    skeptic, desk = build_desk(cfg, journal)
    runners = {}
    if cfg.trade_us and cfg.broker_trading:
        # Real orders at Alpaca (paper or live account); broker = source of truth.
        from botv2.alpaca_ledger import AlpacaClient, AlpacaLedger
        client = AlpacaClient(cfg.alpaca_key, cfg.alpaca_secret, cfg.alpaca_base_url)
        runners["US"] = MarketRunner(
            "US", "USD", cfg, journal, ai,
            AlpacaLedger(journal, "US", client, cfg.max_order_usd),
            skeptic=skeptic, approvals=desk,
        )
    elif cfg.trade_us:
        runners["US"] = MarketRunner(
            "US", "USD", cfg, journal, ai,
            PaperLedger(journal, "US", cfg.us_start_cash),
            alpaca=AlpacaMirror(cfg.alpaca_key, cfg.alpaca_secret, cfg.alpaca_base_url),
            skeptic=skeptic, approvals=desk,
            suggest_only=cfg.us_mode == "suggest",
        )
    if cfg.trade_india:
        runners["INDIA"] = MarketRunner(
            "INDIA", "INR", cfg, journal, ai,
            PaperLedger(journal, "INDIA", cfg.india_start_cash),
            skeptic=skeptic, approvals=desk,
            suggest_only=cfg.india_mode == "suggest",
        )
    if desk:
        desk.runners = runners
    return runners, desk


def build_options(cfg, journal, desk):
    """Paper options desk, or None. Proposals need the approval desk."""
    if not (cfg.options_enabled and cfg.trade_us) or cfg.broker_trading:
        return None
    from botv2.options_desk import OptionsDesk
    opt = OptionsDesk(journal, cfg)
    opt.approvals = desk
    if desk:
        desk.options = opt
    return opt


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
    from botv2 import logsafe
    logsafe.install()                 # no tokens or API keys in the logs
    log = logging.getLogger("botv2.main")
    errs = check_broker_config(cfg)
    if errs:
        raise SystemExit("Refusing to start in broker mode:\n  - " + "\n  - ".join(errs))
    if cfg.broker_trading:
        notify.PREFIX = "💵 LIVE " if cfg.live_real_money else "🧪 ALPACA-PAPER "
    journal = make_journal(cfg.db_path)
    runners, desk = build_runners(cfg, journal)
    options = build_options(cfg, journal, desk)
    if cfg.broker_trading:
        log.info(runners["US"].ledger.preflight())   # bad keys / blocked account -> crash, no trading
    skeptic = next((r.skeptic for r in runners.values()), None)

    args = sys.argv[1:]
    if args[:1] == ["once"]:
        market = (args[1] if len(args) > 1 else "us").upper()
        runners[market].run_cycle()
        return
    if args[:1] == ["review"]:
        for r in runners.values():
            r.weekly_review()
        return
    if args[:1] == ["pending"]:
        for r in (desk.pending() if desk else []):
            print(f"{r['id']}  {r['market']} {r['kind']} {r['symbol']} @ {r['price']} "
                  f"stop {r['stop']} target {r.get('target')}")
        return
    if args[:1] in (["approve"], ["reject"]) and len(args) > 1:
        if not desk:
            print("APPROVAL_MODE is off")
            return
        print(desk.decide(args[1], approve=args[0] == "approve", source="cli"))
        return
    if args[:1] == ["reset-kill"]:
        market = (args[1] if len(args) > 1 else "us").upper()
        print(control.reset_kill_switch(runners[market])[1])
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
            except Exception as exc:
                # A failed cycle silently loses a trading day. On 2026-08-26
                # three cycles died unnoticed for three days. Always alert.
                log.exception("scheduled job %s failed", name)
                try:
                    notify.alert(
                        "[FAILED] " + name + " - no decision made this cycle\n"
                        + type(exc).__name__ + ": " + str(exc)[:250])
                except Exception:
                    pass

    started = time.time()

    def cycle(market):
        def _go():
            if control.is_paused(journal, market):
                log.info("%s paused from dashboard — decision cycle skipped", market)
                return
            runners[market].run_cycle()
            control.mark(journal, market, "cycle")
            if market == "US" and options:
                try:
                    options.propose_from_today()       # one call per idea that passed today
                except Exception:
                    log.exception("options proposals failed")
        return _go

    def monitor(market):
        def _go():
            runners[market].monitor_positions()
            control.mark(journal, market, "monitor")
            if market == "US" and options:
                try:
                    options.monitor()                  # targets and pre-expiry exits
                except Exception:
                    log.exception("options monitor failed")
        return _go

    log.info("TRADE_BOT_V2 scheduler running (markets: %s)", ", ".join(runners))
    notify.alert(f"🟢 TRADE_BOT_V3 started (markets: {', '.join(runners)}) | "
                 f"skeptic {'on' if cfg.skeptic_enabled else 'off'} | "
                 f"approvals {cfg.approval_mode}"
                 + "".join(f" | {m}: suggestions only" for m, r in runners.items() if r.suggest_only)
                 + (f" | options: paper, ${cfg.opt_budget_usd:g}/trade" if options else ""))

    # A transient network blip (e.g. Supabase 'connection reset by peer') must
    # never kill the scheduler: on 2026-08-06 one such error crashed the process
    # and systemd's start-limit left the bot down for a week, unmonitored.
    fails = 0
    while True:
        try:
            journal.kv_set("heartbeat", str(time.time()))  # dashboard "bot online" signal
            control.apply_flags(journal, runners, skeptic, desk, cfg)
            control.drain(journal, runners, desk)          # dashboard commands
            control.write_status(journal, runners, started, MARKETS)
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
                    fire_once(f"{market}_cycle", day, cycle(market))
                # stop/target monitor every 20 min during the session
                if open_at <= hm <= close_at and local.minute % 20 == 0:
                    fire_once(f"{market}_mon", f"{day}T{local.hour}:{local.minute}",
                              monitor(market))
            # weekly self-review: Saturday 08:00 UTC
            if now_utc.weekday() == 5 and (now_utc.hour, now_utc.minute) == (8, 0):
                for r in runners.values():
                    fire_once("review", now_utc.strftime("%Y-%m-%d"), r.weekly_review)
            if desk:
                desk.poll()   # Approve/Skip taps, expiry
            fails = 0
        except Exception:
            fails += 1
            log.exception("scheduler loop iteration failed (%d in a row)", fails)
            if fails in (5, 30, 180):  # ~2 min, ~10 min, ~1 hour of failure
                try:
                    notify.alert(f"⚠️ TRADE_BOT_V2 loop failing — {fails} consecutive "
                                 f"errors (bot alive, still retrying)")
                except Exception:
                    pass
        time.sleep(20)


if __name__ == "__main__":
    main()
