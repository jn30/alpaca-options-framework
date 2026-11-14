#!/usr/bin/env python3
"""
Spread bot: trades two SPY rule sets on the Alpaca PAPER account and sizes
them from the account's cash.

    Nectar  iron condor. Monday (else Tuesday). Short put $2.50 under SPY with a
            $5 wing, short call 3% over SPY with a $3 wing. Expires Thursday
            (Friday for a Tuesday entry). Bought back at 75% of the credit.
    Comb    put credit spread. Monday. Short put nearest 15 delta, long put $10
            below, expiring the next Friday at least 7 days out. Bought back at
            50% of the credit.

Both: no stop-loss; closed 30 minutes before the bell on expiry day if the
buy-back never filled; one position per strategy at a time.

Sizing: the worst-case loss of all open positions together may use the whole
cash balance (no margin). Cash is split 7% Nectar / 93% Comb; a strategy that
cannot afford one contract hands its share to the other (spread_rules.allocate).

The rules are NOT backtested. Related tests in this repo (0DTE condor, 30-45 DTE
put spreads) were negative.

    python spread_bot.py                 # loop forever (orders if SPREAD_SUBMIT)
    python spread_bot.py --now           # one pass right now, dry-run
    python spread_bot.py --now --submit  # one pass right now, real paper orders
    python spread_bot.py --status        # state file + account
    python spread_bot.py --clear-halt    # resume entries after a manual check

Spec: docs/superpowers/specs/2026-11-14-spread-bot-design.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import socket
import sys

import config

logger = logging.getLogger("spread_bot")
for _s in (sys.stdout, sys.stderr):   # Windows consoles default to cp1252
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ET = config.ET
LOCK_PORT = 47321          # same port as trend_bot.py: the two must never run together
_LOCK = None
STATE_PATH = os.path.join(config.LOG_DIR, "spread_state.json")
CSV_PATH = os.path.join(config.LOG_DIR, "spread_trades.csv")
STRATEGIES = ("nectar", "comb")


# ── State ─────────────────────────────────────────────────────────────────────

def empty_state() -> dict:
    return {"nectar": None, "comb": None, "skipped": {}, "halted": None}


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return empty_state()
    return {**empty_state(), **st}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_PATH)


# ── Start-up guards ───────────────────────────────────────────────────────────

def guard():
    """Exit code 2 (with a message) if this bot must not run, else None."""
    if config.STRATEGY != "spreads":
        print(f"config.STRATEGY is '{config.STRATEGY}', not 'spreads' — refusing to run (one strategy "
              f"per account). Set STRATEGY = \"spreads\" or BOT_STRATEGY=spreads.", file=sys.stderr)
        return 2
    if config.PAPER is not True:
        print("Refusing: config.PAPER is not True. The spread bot is paper-only.", file=sys.stderr)
        return 2
    return None


def take_lock() -> bool:
    global _LOCK                       # keep the socket open for the process lifetime
    _LOCK = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _LOCK.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        return False
    return True


# ── Entry point ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--now", action="store_true", help="one pass right now, then exit (dry-run unless --submit)")
    ap.add_argument("--submit", action="store_true", help="with --now: send real PAPER orders")
    ap.add_argument("--status", action="store_true", help="print the state file and the account, then exit")
    ap.add_argument("--clear-halt", action="store_true", help="clear a halt (assignment / leg mismatch)")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    rc = guard()
    if rc is not None:
        return rc
    os.makedirs(config.LOG_DIR, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(os.path.join(config.LOG_DIR, "spread_bot.log"),
                                                      encoding="utf-8")])
    if args.clear_halt:
        st = load_state()
        logger.info("halt cleared (was: %s)", st["halted"])
        st["halted"] = None
        save_state(st)
        return 0
    if args.status:
        print(json.dumps(load_state(), indent=1))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
