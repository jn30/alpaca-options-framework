import os
from zoneinfo import ZoneInfo

# ── Secrets: loaded from .env next to this file (gitignored — never commit) ─────
# .env format, one per line:  ALPACA_API_KEY=...  ALPACA_API_SECRET=...  DISCORD_WEBHOOK_URL=...
# Real environment variables take precedence over .env.
def _load_dotenv(path: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_dotenv()

# ── API credentials ────────────────────────────────────────────────────────────
ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY", "YOUR_ALPACA_API_KEY_HERE")
ALPACA_API_SECRET = os.environ.get("ALPACA_API_SECRET", "YOUR_ALPACA_API_SECRET_HERE")
PAPER             = True   # set False only after paper validation passes

# ── Discord: every individual trade is posted here (empty = off) ───────────────
# Treat this URL like a password: anyone who has it can post to your channel.
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# ── Which strategy runs on this account ────────────────────────────────────────
# "directional" -> main.py          (0DTE momentum calls/puts)
# "condor"      -> condor_paper.py  (one 0DTE SPY iron condor per session)
# Only ONE strategy may run per account: the directional bot closes any option
# position it doesn't track ("ghost sweep") and cancels all open option orders,
# which would tear a condor apart. Each entry point refuses to start unless this
# matches. Override per run without editing the file:  BOT_STRATEGY=directional
STRATEGY          = os.environ.get("BOT_STRATEGY", "condor").strip().lower()

# Iron-condor settings (used by condor_paper.py; CLI flags override these)
CONDOR_ENTRY_TIME   = "13:00"   # ET
CONDOR_DISTANCE     = 2.0       # short strikes this many $ from spot
CONDOR_WIDTH        = 3.0       # spread width in $
CONDOR_QTY          = 1         # contracts per condor when CONDOR_SIZING = "fixed"
CONDOR_UNDERLYING   = "SPY"     # "SPY" | "SPXW" | "auto" (auto = SPXW once equity >= CONDOR_AUTO_MIN_EQUITY)
CONDOR_AUTO_MIN_EQUITY = 5000   # account equity at which "auto" switches SPY -> SPXW
CONDOR_SPX_SCALE    = 10        # SPXW structure = SPY structure x this (2$/3$ -> 20/30 pts)
CONDOR_SPX_STEP     = 5         # SPXW strike increment
CONDOR_SPX_SHADOW   = True      # while trading SPY, log the SPXW twin's quotes/spreads each day (no orders)
CONDOR_SIZING       = "scaled"  # "scaled" = 1 condor per CONDOR_EQUITY_PER_CONTRACT of equity (min 1) | "fixed" = CONDOR_QTY | "max" = all buying power (ruined a $1000 account in the backtest)
CONDOR_EQUITY_PER_CONTRACT = 3000   # backtest: 1 contract on $1000 = max drawdown -25%; 3 contracts = -98% (ruin)
CONDOR_BP_FRACTION  = 0.95      # share of buying power "max" may use (buffer for fills/rounding)
CONDOR_EXIT         = "expiry"  # "expiry" (hold, log settlement) or "close1555"
CONDOR_MAX_CONCEDE  = 3         # cents conceded from mid while working the entry
CONDOR_MIN_CREDIT   = 0.25      # skip the session if mid credit is below this
CONDOR_MAX_CREDIT_FRAC = 0.60  # sanity: skip if mid credit exceeds this share of the width (bad quotes)
CONDOR_LOOP         = True      # keep the process running: one condor per trading day, no daily restart
CONDOR_SUBMIT       = True      # False = dry-run (prints the order); True = send real PAPER orders

# ── Universe ───────────────────────────────────────────────────────────────────
UNDERLYING        = "SPY"

# ── Strike selection (pre-market) ──────────────────────────────────────────────
# Target strike = round(SPY_price + min(ATR_MULT * ATR_5day, MAX_STRIKE_OFFSET)) to nearest STRIKE_STEP
ATR_MULT          = 0.60   # how far OTM to target (calibrate after backtesting)
MAX_STRIKE_OFFSET = 4.50   # hard cap on strike distance — prevents elevated 5-day ATR from
                            # placing strikes so far OTM that SPY never reaches the approach zone
STRIKE_STEP       = 1.00   # SPY strikes are in $0.50 increments
STRIKE_ALTS       = 5      # extra strikes above/below — wide window, never resubscribe

# ── Entry filters ──────────────────────────────────────────────────────────────
ENTRY_START       = "09:45"  # ET — ignore signals before this
ENTRY_END         = "12:00"  # ET — no new entries after noon (afternoons net -$514/47 trades in Jul; edge concentrates AM). Was 14:30.
TIME_STOP         = "15:25"  # ET — force-close all positions

OPTION_MIN_PRICE  = 0.20   # min price — filters deeply OTM lottery tickets
OPTION_MAX_PRICE  = 10.00  # max price

# Momentum thresholds
MIN_CONSEC_BARS   = 3      # consecutive green (or red) 1-min bars required
ROC_THRESHOLD     = 0.0003 # 5-bar rate-of-change minimum
ATR5_MIN_ENTRY    = 0.20   # min 5-bar ATR at entry — blocks low-vol theta-bleed setups

# Strike proximity zones
ACTIVATION_PCT    = 0.003  # within 0.3% of strike → "activation" zone (~$2.20 at SPY $740)
APPROACH_PCT      = 0.007  # within 0.7% → "approach" zone (~$5.20 at SPY $740)

# Proxy delta — disabled: delta only updates on 1-min bar ticks so stays 0 between
# bars and blocks all entries. Momentum + zone filters are sufficient gatekeepers.
PROXY_DELTA_MIN   = 0.0    # 0.0 = disabled (was 0.03)
REQUIRE_DELTA_RISING = False

# ── Risk & sizing ──────────────────────────────────────────────────────────────
MAX_RISK_PER_TRADE = 150.00   # dollars at risk per trade (stop loss basis)
MAX_DAILY_LOSS     = 300.00   # hard daily loss limit — bot shuts down entries
# Dynamic sizing: when True, the three limits below scale with start-of-day account
# equity (read from Alpaca at startup) instead of the fixed dollar values above, and
# position size is also capped by options buying power. The fixed-dollar values stay
# as the fallback if the account can't be read, and are what the backtest uses.
DYNAMIC_RISK         = True
MAX_RISK_PCT         = 0.05    # risk per trade (stop-loss basis) as a fraction of equity
MAX_DAILY_LOSS_PCT   = 0.10    # daily loss lock
PROFIT_TARGET_PCT    = 0.04    # daily profit-target lock ("keep the money")
MAX_TRADES_PER_DAY = 999      # effectively unlimited during paper/data collection
TRADE_COOLDOWN_BARS = 3       # 1-min bars to wait after a trade closes before re-entering

# Daily profit target — once realized daily P&L reaches this, stop NEW entries
# for the session ("keep the money"). 0 disables. Open positions still exit normally.
PROFIT_TARGET      = 125.00
# Back-to-back win lock — after this many consecutive winning trades (realized
# P&L > 0), stop NEW entries for the session. Any loss resets the streak. 0 disables.
CONSEC_WIN_STOP    = 3

# ── Bracket order exit levels ──────────────────────────────────────────────────
STOP_MULT          = 0.50   # hard stop:          exit if price falls to 50% of entry
TP_MULT            = 1.50   # take-profit:        exit at 50% gain (1.5× entry)

# Peak trailing stop — locks in recoveries
# Activates once the option has gained PEAK_TRAIL_ACTIVATE above entry.
# From that point, trails at PEAK_TRAIL_PCT × the highest mid seen.
# Example: entry=$0.96, peak=$1.15 → trail fires at $1.012 (near BE)
PEAK_TRAIL_ACTIVATE = 1.20  # trail arm: option must reach 20% gain before trail is active
PEAK_TRAIL_PCT      = 0.88  # trail stop: exit if mid falls to 88% of peak

# ── SPY-level stop ────────────────────────────────────────────────────────────
# Fires on bar close if SPY has closed SPY_STOP_ATR_MULT × atr5_at_entry dollars
# past the entry SPY price against the position (call: SPY falls, put: SPY rises).
# Buffer scales with intrabar volatility at entry — tighter when market is calm,
# wider when choppy — avoiding the whipsaw false-stops a fixed dollar amount causes.
# SPY_STOP_FLOOR is the minimum buffer regardless of atr5 (guards against
# near-zero early-session atr5 before enough bars have accumulated).
SPY_STOP_ATR_MULT  = 0.75  # buffer = 0.75 × atr5 at entry time
SPY_STOP_FLOOR     = 0.10  # minimum buffer in dollars

# ── Polling ────────────────────────────────────────────────────────────────────
SNAPSHOT_POLL_SEC  = 30    # how often to poll REST snapshot for proxy-delta calc

# ── Misc ───────────────────────────────────────────────────────────────────────
ET                 = ZoneInfo("America/New_York")
LOG_DIR            = "logs"
