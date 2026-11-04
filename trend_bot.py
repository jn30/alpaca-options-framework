#!/usr/bin/env python3
"""
Trend bot: 2x S&P 500 (SSO) while SPY is above its 200-day SMA, else T-bills (SGOV).

Backtest (backtest/leveraged_trend.py, 1988-2026, pre-registered primary):
    2x + SMA200   CAGR 12.8%  max DD -51%   vs   S&P 500 buy & hold 11.4% / -55%
    (synthetic 2x ran ~0.35%/yr above the real SSO 2009-2026; the margin over
    buy & hold is ~1.2%/yr after that -- real, but thin; the price-only test
    from 1970 beat buy & hold over the full period but not in 2 of 3 sub-periods)

Once per trading day, 15 minutes before the close:
    signal = SPY last price > SMA200 (199 prior daily closes + today's price)
    target = TREND_RISK_ON (SSO) if signal else TREND_RISK_OFF (SGOV)
    if not fully in the target: sell the other one, buy the target with all cash
It touches only the two symbols above and refuses to run next to the option
bots (one strategy per account).

    python trend_bot.py            # loop forever (default), one check per day
    python trend_bot.py --now      # one check right now (respects TREND_SUBMIT)
    python trend_bot.py --dry      # print the decision, send nothing
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import os
import sys
import time

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import GetCalendarRequest, MarketOrderRequest

import config
from notifier import notify

logger = logging.getLogger("trend_bot")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ET = config.ET
TRADES_CSV = os.path.join(config.LOG_DIR, "trend_trades.csv")
DAILY_CSV = os.path.join(config.LOG_DIR, "trend_daily.csv")


def sma_signal(stock: StockHistoricalDataClient, today: dt.date) -> tuple[bool, float, float]:
    """(above, last price, SMA) with today's live price as today's close."""
    req = StockBarsRequest(
        symbol_or_symbols=config.TREND_SIGNAL, timeframe=TimeFrame.Day, feed=DataFeed.SIP,
        start=dt.datetime.combine(today - dt.timedelta(days=400), dt.time(0, 0), tzinfo=ET),
        end=dt.datetime.combine(today - dt.timedelta(days=1), dt.time(23, 59), tzinfo=ET))
    bars = [b for b in stock.get_stock_bars(req).data.get(config.TREND_SIGNAL, [])
            if b.timestamp.astimezone(ET).date() < today]
    n = config.TREND_SMA
    if len(bars) < n - 1:
        raise RuntimeError(f"only {len(bars)} daily bars for {config.TREND_SIGNAL}, need {n - 1}")
    last = float(stock.get_stock_latest_trade(StockLatestTradeRequest(
        symbol_or_symbols=config.TREND_SIGNAL, feed=DataFeed.IEX))[config.TREND_SIGNAL].price)
    closes = [float(b.close) for b in bars[-(n - 1):]] + [last]
    sma = sum(closes) / n
    return last > sma, last, sma


def holdings(trading: TradingClient) -> dict[str, float]:
    """Market value of the two managed symbols."""
    out = {config.TREND_RISK_ON: 0.0, config.TREND_RISK_OFF: 0.0}
    for p in trading.get_all_positions():
        if p.symbol in out:
            out[p.symbol] = float(p.market_value)
    return out


def log_csv(path: str, header: list[str], row: list) -> None:
    os.makedirs(config.LOG_DIR, exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def wait_order(trading: TradingClient, oid, seconds: int = 60):
    end = time.time() + seconds
    o = trading.get_order_by_id(oid)
    while time.time() < end and o.status not in ("filled", "canceled", "expired", "rejected"):
        time.sleep(2)
        o = trading.get_order_by_id(oid)
    return o


def rebalance(trading: TradingClient, target: str, other: str, submit: bool) -> str:
    acct = trading.get_account()
    equity, cash = float(acct.equity), float(acct.cash)
    held = holdings(trading)
    actions = []
    if held[other] > 1.0:
        actions.append(f"SELL all {other} (${held[other]:.2f})")
        if submit:
            o = trading.close_position(other)
            o = wait_order(trading, o.id)
            logger.info("SELL %s -> %s, filled %s @ %s", other, o.status, o.filled_qty, o.filled_avg_price)
            log_csv(TRADES_CSV, ["time_et", "symbol", "side", "qty", "price", "status"],
                    [dt.datetime.now(ET).isoformat(timespec="seconds"), other, "sell",
                     o.filled_qty, o.filled_avg_price, o.status])
            time.sleep(3)
            cash = float(trading.get_account().cash)
    buy = round(cash * config.TREND_CASH_USE, 2)
    if buy >= max(1.0, equity * config.TREND_MIN_TRADE_FRAC):
        actions.append(f"BUY {target} for ${buy:.2f}")
        if submit:
            o = trading.submit_order(MarketOrderRequest(
                symbol=target, notional=buy, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
            o = wait_order(trading, o.id)
            logger.info("BUY %s $%.2f -> %s, filled %s @ %s", target, buy, o.status, o.filled_qty,
                        o.filled_avg_price)
            log_csv(TRADES_CSV, ["time_et", "symbol", "side", "qty", "price", "status"],
                    [dt.datetime.now(ET).isoformat(timespec="seconds"), target, "buy",
                     o.filled_qty, o.filled_avg_price, o.status])
    return "; ".join(actions) or "nothing to do"


def run_check(stock, trading, submit: bool) -> None:
    today = dt.datetime.now(ET).date()
    above, last, sma = sma_signal(stock, today)
    target, other = ((config.TREND_RISK_ON, config.TREND_RISK_OFF) if above
                     else (config.TREND_RISK_OFF, config.TREND_RISK_ON))
    before = holdings(trading)
    switched = before[other] > 1.0
    result = rebalance(trading, target, other, submit)
    eq = float(trading.get_account().equity)
    logger.info("%s %.2f vs SMA%d %.2f -> %s | %s | equity $%.2f%s", config.TREND_SIGNAL, last,
                config.TREND_SMA, sma, target, result, eq, "" if submit else " (DRY)")
    log_csv(DAILY_CSV, ["date", "spy", "sma", "signal", "target", "action", "equity"],
            [today.isoformat(), f"{last:.2f}", f"{sma:.2f}", int(above), target, result, f"{eq:.2f}"])
    if switched or result.startswith("BUY") and before[target] < 1.0:
        notify(f"{'📈' if above else '🛡️'} **TREND-Bot {today}**: {config.TREND_SIGNAL} {last:.2f} "
               f"{'über' if above else 'unter'} SMA{config.TREND_SMA} {sma:.2f} → **{target}**  "
               f"({result}{'' if submit else ', DRY'})  Equity ${eq:.2f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--now", action="store_true", help="one check right now, then exit")
    ap.add_argument("--dry", action="store_true", help="never send orders")
    args = ap.parse_args()
    submit = config.TREND_SUBMIT and not args.dry

    if config.STRATEGY != "trend":
        print(f"config.STRATEGY is '{config.STRATEGY}', not 'trend' — refusing to run (one strategy "
              f"per account). Set STRATEGY = \"trend\" or BOT_STRATEGY=trend.", file=sys.stderr)
        return 2
    if submit and not config.PAPER:
        print("Refusing: config.PAPER is False. Validate on paper first.", file=sys.stderr)
        return 2
    os.makedirs(config.LOG_DIR, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(os.path.join(config.LOG_DIR, "trend_bot.log"),
                                                      encoding="utf-8")])
    stock = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)
    trading = TradingClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET, paper=config.PAPER)

    others = [p.symbol for p in trading.get_all_positions()
              if p.symbol not in (config.TREND_RISK_ON, config.TREND_RISK_OFF)]
    if others:
        logger.warning("Account holds other positions the trend bot will NOT touch: %s", others)

    if args.now:
        run_check(stock, trading, submit)
        return 0

    logger.info("Trend bot loop: %s/%s on %s vs SMA%d, check %d min before the close%s.",
                config.TREND_RISK_ON, config.TREND_RISK_OFF, config.TREND_SIGNAL, config.TREND_SMA,
                config.TREND_MINUTES_BEFORE_CLOSE, "" if submit else " (DRY)")
    done_for = None
    while True:
        try:
            now = dt.datetime.now(ET)
            if done_for != now.date():
                cal = trading.get_calendar(GetCalendarRequest(start=now.date(), end=now.date()))
                if cal:
                    close = cal[0].close
                    close = close.replace(tzinfo=ET) if close.tzinfo is None else close.astimezone(ET)
                    at = close - dt.timedelta(minutes=config.TREND_MINUTES_BEFORE_CLOSE)
                    if at <= now < close:
                        run_check(stock, trading, submit)
                        done_for = now.date()
                    elif now >= close:
                        done_for = now.date()
                else:
                    done_for = now.date()
        except Exception as exc:
            logger.exception("check failed")
            notify(f"🚨 **TREND-Bot Fehler:** `{type(exc).__name__}: {str(exc)[:300]}`")
            time.sleep(120)
        time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
