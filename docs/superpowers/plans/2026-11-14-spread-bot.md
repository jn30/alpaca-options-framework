# Spread-Bot (Nectar + Comb) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ein Paper-Bot, der die SPY-Regelwerke Nectar (Iron Condor) und Comb (Put-Credit-Spread) handelt und die Kontraktzahl aus dem Bargeld des Kontos ableitet.

**Architecture:** `spread_rules.py` enthält alle Regeln als reine Funktionen ohne Alpaca-Import (Strikes, Verfall, Größe, Rückkaufpreis). `spread_bot.py` ist der Broker-Teil: Kurse holen, Orders arbeiten, Zustand führen, Tagesschleife. Muster und Hilfen (`work_order`-Logik, Sperr-Port, Zustandsdatei, `notify`) kommen aus `condor_paper.py` und `trend_bot.py`.

**Tech Stack:** Python 3.9+, alpaca-py 0.43, pytest 9.

**Spec:** `docs/superpowers/specs/2026-11-14-spread-bot-design.md`

## Global Constraints

- Nur Paper: Start verweigern (Exit 2), wenn `config.PAPER` nicht `True` ist oder `config.STRATEGY != "spreads"`.
- Summe (Flügelbreite × 100 × Kontrakte) aller offenen Positionen ≤ `account.cash`; zusätzlich ≤ `options_buying_power`. Nie Margin.
- Alle Orders sind Mehrbein-Limit-Orders (`OrderClass.MLEG`, `TimeInForce.DAY`). Nie ein Short-Bein einzeln.
- Pro Strategie höchstens eine offene Position.
- Keine Position bleibt über ihren Verfallstag hinaus offen.
- Kein Stop-Loss, keine zusätzlichen Filter.
- Bestehende Dateien außer `config.py` und `README.md` nicht ändern. `condor_paper.py` wird importiert, nicht umgebaut.
- Arbeit auf Branch `feat/spread-bot`, nicht auf `main`.
- Discord-Texte auf Deutsch, Code und Kommentare auf Englisch (wie im Projekt).

## Review Focus

1. **Neustart, Position inzwischen weg:** Zustand sagt "offen", Konto hält die Beine nicht mehr (Rückkauf-Order lief durch, während der Bot aus war). Erwartet: als zurückgekauft verbuchen, nichts schließen wollen. Test in Task 5.
2. **Kein brauchbarer Kurs beim Einstieg** (Bid 0, Bein nicht gelistet): Erwartet: diese Woche aussetzen und melden, keine Absturzschleife. Test in Task 4.
3. **Verkürzter Handelstag am Verfall** (Börsenschluss 13:00 ET): Erwartet: 30 Minuten vor dem tatsächlichen Schluss schließen, nicht um 15:30. Test in Task 1.
4. **Credit ≤ 0 oder ≥ Flügelbreite:** Erwartet: 0 Kontrakte, nie Division durch 0 oder negative Stückzahl. Test in Task 2.
5. **Montag ist Feiertag:** Erwartet: Nectar steigt Dienstag mit Freitags-Verfall ein, Comb setzt die Woche aus. Test in Task 1.

---

## Dateien

| Datei | Aufgabe |
|---|---|
| `spread_rules.py` (neu) | Reine Regeln, keine Netzwerkzugriffe |
| `spread_bot.py` (neu) | Broker, Zustand, Schleife, CLI |
| `tests/test_spread_rules.py` (neu) | Tests der Regeln |
| `tests/test_spread_bot.py` (neu) | Tests der Zustandslogik mit Fake-Clients |
| `config.py` (ändern) | `SPREAD_*`-Block, `STRATEGY`-Kommentar |
| `run_spreads.bat` (neu) | Dauerlauf wie `run_trend.bat` |
| `README.md` (ändern) | Abschnitt "Spread bot" |

---

### Task 1: Regeln für Strikes, Verfall, Rückkauf

**Files:**
- Create: `spread_rules.py`
- Test: `tests/test_spread_rules.py`

**Interfaces:**
- Produces:
  - `nectar_strikes(spot: float) -> dict[str, float]` mit Schlüsseln `short_put, long_put, short_call, long_call`
  - `nectar_entry_ok(day: date, trading_days: set[date]) -> bool` — Montag, oder Dienstag wenn der Montag derselben Woche kein Handelstag ist. (Dienstag nach erfolglosem Montag entscheidet der Bot über den Zustand, Task 6.)
  - `nectar_expiry(entry: date, trading_days: set[date]) -> date`
  - `comb_expiry(entry: date, trading_days: set[date]) -> date`
  - `pick_delta_strike(deltas: dict[float, float], target: float = -0.15) -> float | None`
  - `buyback_price(credit: float, frac: float) -> float`
  - `expiry_close_time(session_close: time) -> time`

- [ ] **Step 1: Tests schreiben**

```python
import datetime as dt
import spread_rules as r

D = dt.date
WEEK = {D(2026, 11, 16), D(2026, 11, 17), D(2026, 11, 18), D(2026, 11, 19), D(2026, 11, 20),
        D(2026, 11, 23), D(2026, 11, 24), D(2026, 11, 25), D(2026, 11, 27)}   # 26th = Thanksgiving

def test_nectar_strikes_basic():
    assert r.nectar_strikes(672.40) == {"short_put": 670, "long_put": 665,
                                        "short_call": 693, "long_call": 696}

def test_nectar_short_put_must_be_below_spot_and_call_above():
    s = r.nectar_strikes(2.60)          # round(0.10) = 0 -> still below; degenerate guard only
    assert s["short_put"] < 2.60 and s["short_call"] > 2.60

def test_nectar_expiry_monday_is_thursday_tuesday_is_friday():
    assert r.nectar_expiry(D(2026, 11, 16), WEEK) == D(2026, 11, 19)
    assert r.nectar_expiry(D(2026, 11, 17), WEEK) == D(2026, 11, 20)

def test_nectar_expiry_holiday_thursday_moves_to_friday():
    assert r.nectar_expiry(D(2026, 11, 23), WEEK) == D(2026, 11, 27)

def test_nectar_entry_days():
    assert r.nectar_entry_ok(D(2026, 11, 16), WEEK)
    assert not r.nectar_entry_ok(D(2026, 11, 17), WEEK)            # Monday traded
    assert r.nectar_entry_ok(D(2026, 11, 17), WEEK - {D(2026, 11, 16)})   # Monday holiday
    assert not r.nectar_entry_ok(D(2026, 11, 18), WEEK)

def test_comb_expiry_is_next_friday_with_min_7_dte():
    assert r.comb_expiry(D(2026, 11, 16), WEEK) == D(2026, 11, 27)   # 11 days
    assert r.comb_expiry(D(2026, 11, 20), WEEK) == D(2026, 11, 27)   # exactly 7 days

def test_comb_expiry_holiday_friday_uses_last_trading_day_before():
    days = WEEK - {D(2026, 11, 27)}
    assert r.comb_expiry(D(2026, 11, 16), days) == D(2026, 11, 25)

def test_pick_delta_strike():
    assert r.pick_delta_strike({660: -0.11, 662: -0.14, 663: -0.17, 665: -0.22}) == 662
    assert r.pick_delta_strike({}) is None

def test_buyback_price_floors_to_cent():
    assert r.buyback_price(1.60, 0.75) == 1.20
    assert r.buyback_price(0.50, 0.50) == 0.25
    assert r.buyback_price(0.37, 0.50) == 0.18

def test_expiry_close_time_normal_and_half_day():
    assert r.expiry_close_time(dt.time(16, 0)) == dt.time(15, 30)
    assert r.expiry_close_time(dt.time(13, 0)) == dt.time(12, 30)
```

- [ ] **Step 2: Tests laufen lassen, Fehlschlag sehen**

Run: `python -m pytest tests/test_spread_rules.py -v`
Expected: FAIL, `ModuleNotFoundError: spread_rules`

- [ ] **Step 3: `spread_rules.py` implementieren**

Festgelegte Werte: Short Put = `round(spot - 2.50)`, falls `>= spot` dann 1 tiefer; Long Put 5 darunter. Short Call = `round(spot * 1.03)`, falls `<= spot` dann 1 höher; Long Call 3 darüber. Verfall bei Feiertag: nächster Handelstag derselben Woche (Mo–Fr), sonst der letzte Handelstag davor. Comb: erster Freitag mit `(friday - entry).days >= 7`, dann dieselbe Feiertagsregel. `buyback_price` rundet vor dem Abrunden auf 6 Stellen, damit `1.60 * 0.75` nicht auf 1.19 fällt. `expiry_close_time` = 30 Minuten vor Börsenschluss.

- [ ] **Step 4: Tests laufen lassen**

Run: `python -m pytest tests/test_spread_rules.py -v`
Expected: alle PASS

- [ ] **Step 5: Commit**

```bash
git add spread_rules.py tests/test_spread_rules.py
git commit -m "feat: spread rules (Nectar/Comb strikes, expiries, buy-back price)"
```

---

### Task 2: Größe und Aufteilung

**Files:**
- Modify: `spread_rules.py`
- Test: `tests/test_spread_rules.py`

**Interfaces:**
- Produces:
  - `contracts(budget: float, width: float, credit: float) -> int`
  - `allocate(capital: float, loss: dict[str, float], shares: dict[str, float]) -> dict[str, int]` — beide Strategien frei; `loss` = Maximalverlust pro Kontrakt je Strategie (`"nectar"`, `"comb"`), fehlender Schlüssel = Strategie kann heute nicht einsteigen.
  - `qty_alongside(capital: float, free_cash: float, share: float, loss_per_contract: float) -> int` — die andere Strategie ist schon offen.

- [ ] **Step 1: Tests schreiben**

```python
SH = {"nectar": 0.07, "comb": 0.93}

def test_contracts():
    assert r.contracts(871, 5, 1.60) == 2
    assert r.contracts(339, 5, 1.60) == 0
    assert r.contracts(5000, 5, 0) == 0        # no credit
    assert r.contracts(5000, 5, 5.2) == 0      # credit >= width
    assert r.contracts(-10, 5, 1.6) == 0

def test_allocate_original_ratio():
    assert r.allocate(10500, {"nectar": 340, "comb": 950}, SH) == {"nectar": 2, "comb": 10}

def test_allocate_spillover_to_other_strategy():
    assert r.allocate(5000, {"nectar": 340, "comb": 950}, SH) == {"nectar": 1, "comb": 4}
    assert r.allocate(2000, {"nectar": 340, "comb": 950}, SH) == {"nectar": 0, "comb": 2}

def test_allocate_both_zero_tries_comb_then_nectar_with_everything():
    assert r.allocate(871, {"nectar": 340, "comb": 950}, SH) == {"nectar": 2, "comb": 0}
    assert r.allocate(1000, {"nectar": 340, "comb": 950}, SH) == {"nectar": 0, "comb": 1}

def test_allocate_one_strategy_unavailable():
    assert r.allocate(871, {"nectar": 340}, SH) == {"nectar": 2, "comb": 0}

def test_allocate_never_exceeds_capital():
    q = r.allocate(5000, {"nectar": 340, "comb": 950}, SH)
    assert q["nectar"] * 340 + q["comb"] * 950 <= 5000

def test_qty_alongside_capped_by_free_cash():
    assert r.qty_alongside(10000, 500, 0.07, 340) == 1     # share 700 -> 2, free cash allows 1
    assert r.qty_alongside(10000, 5000, 0.07, 340) == 2
    assert r.qty_alongside(10000, 100, 0.07, 340) == 0
```

- [ ] **Step 2: Fehlschlag sehen**

Run: `python -m pytest tests/test_spread_rules.py -v`
Expected: neue Tests FAIL (`AttributeError`)

- [ ] **Step 3: Implementieren**

`allocate`: (1) Kontrakte je Strategie aus `capital * share`. (2) Hat genau eine Strategie 0 (oder fehlt sie in `loss`), bekommt die andere das ganze Kapital. (3) Haben beide 0, bekommt erst Comb das ganze Kapital, und nur wenn das auch 0 ergibt, Nectar. `loss` ist bereits `(width - credit) * 100`; `contracts` rechnet denselben Wert aus Breite und Credit.

- [ ] **Step 4: Tests laufen lassen**

Run: `python -m pytest tests/test_spread_rules.py -v`
Expected: alle PASS

- [ ] **Step 5: Commit**

```bash
git add spread_rules.py tests/test_spread_rules.py
git commit -m "feat: cash-based sizing and 7/93 allocation for the spread bot"
```

---

### Task 3: Konfiguration, Gerüst, Zustand

**Files:**
- Modify: `config.py` (Block nach den `CONDOR_*`-Einstellungen; `STRATEGY`-Kommentar um `"spreads" -> spread_bot.py` ergänzen, Standardwert `"trend"` bleibt)
- Create: `spread_bot.py`
- Test: `tests/test_spread_bot.py`

**Interfaces:**
- Produces (`config.py`):
  ```python
  SPREAD_SHARES        = {"nectar": 0.07, "comb": 0.93}
  SPREAD_ENTRY_TIME    = "10:00"   # ET
  SPREAD_ENTRY_END     = "15:00"   # ET, no new entry after this
  SPREAD_BUYBACK       = {"nectar": 0.75, "comb": 0.50}   # buy back at this share of the credit
  SPREAD_COMB_DELTA    = -0.15
  SPREAD_COMB_WIDTH    = 10.0
  SPREAD_MAX_CONCEDE   = 3         # cents conceded from mid while working an entry
  SPREAD_MAX_CREDIT_FRAC = 0.60    # skip if mid credit exceeds this share of the width (bad quotes)
  SPREAD_SUBMIT        = True      # False = log decisions only
  ```
- Produces (`spread_bot.py`):
  - `STATE_PATH = logs/spread_state.json`, `CSV_PATH = logs/spread_trades.csv`
  - `load_state() -> dict`, `save_state(state: dict) -> None` (atomar wie im Condor-Bot). Leerer Zustand: `{"nectar": None, "comb": None, "skipped": {}, "halted": None}`.
  - Position: `{"strategy", "entry_date", "expiry", "legs": [{"symbol", "short": bool}], "qty", "width", "mid_credit", "credit", "buyback", "tp_order_id", "equity_before"}`
  - `main() -> int` mit `--now` (ein Durchlauf, Trockenlauf außer mit `--submit`), `--submit`, `--status` (Zustand und Konto ausgeben)
  - `guard() -> int | None` — Exit 2 mit Meldung, wenn Strategie/Paper nicht passt

- [ ] **Step 1: Tests schreiben**

```python
import spread_bot as b

def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "STATE_PATH", str(tmp_path / "s.json"))
    assert b.load_state() == {"nectar": None, "comb": None, "skipped": {}, "halted": None}
    st = b.load_state(); st["halted"] = "assignment"; b.save_state(st)
    assert b.load_state()["halted"] == "assignment"

def test_guard_refuses_wrong_strategy(monkeypatch):
    monkeypatch.setattr(b.config, "STRATEGY", "trend")
    assert b.guard() == 2

def test_guard_refuses_live(monkeypatch):
    monkeypatch.setattr(b.config, "STRATEGY", "spreads")
    monkeypatch.setattr(b.config, "PAPER", False)
    assert b.guard() == 2
```

- [ ] **Step 2: Fehlschlag sehen**

Run: `python -m pytest tests/test_spread_bot.py -v`
Expected: FAIL, `ModuleNotFoundError: spread_bot`

- [ ] **Step 3: Implementieren**

Logging nach `logs/spread_bot.log`, UTF-8-Umstellung der Konsole und Sperr-Port wie in `trend_bot.py`. Derselbe Port `47321` wie der Trend-Bot (aus `trend_bot` importieren), damit nie beide laufen; die Sperre gilt nur im Dauerlauf, nicht bei `--now`/`--status`.

- [ ] **Step 4: Tests und Start-Verweigerung prüfen**

Run: `python -m pytest tests -v` → alle PASS
Run: `python spread_bot.py --status` → Exit 2 mit "config.STRATEGY is 'trend', not 'spreads'"

- [ ] **Step 5: Commit**

```bash
git add config.py spread_bot.py tests/test_spread_bot.py
git commit -m "feat: spread bot skeleton (config, guards, state file)"
```

---

### Task 4: Einstieg

**Files:**
- Modify: `spread_bot.py`
- Test: `tests/test_spread_bot.py`

**Interfaces:**
- Consumes: alles aus Task 1–3; `condor_paper.leg_quotes`, `condor_paper._poll`, `strikes.build_occ_symbol`, `notifier.notify`
- Produces:
  - `build_order(legs: list[dict], qty: int, price: float, opening: bool) -> LimitOrderRequest` — beliebig viele Beine; Vorzeichen-Konvention wie `condor_paper.build_order` (Credit negativ, Debit positiv)
  - `work(trading, legs, qty, start_price, opening, max_steps, step=0.01, step_wait=20, cap=None)` — gleiche Logik wie `condor_paper.work_order`, aber auf `build_order` oben
  - `credit_of(legs: list[dict], quotes: dict[str, tuple[float, float]]) -> tuple[float, float]` — (Mid, Natural)
  - `plan_nectar(stock, opt, trading, day) -> dict | None` und `plan_comb(stock, opt, trading, day) -> dict | None` — liefern `{"strategy", "expiry", "legs", "width", "mid_credit"}` oder `None` mit geloggtem Grund
  - `enter(trading, plan: dict, qty: int, submit: bool) -> dict | None` — arbeitet die Order, liefert die Position (Task 3) oder `None`

- [ ] **Step 1: Tests schreiben** (Fake-Clients, kein Netz)

```python
LEGS = [{"symbol": "SPY261127P00650000", "short": True},
        {"symbol": "SPY261127P00640000", "short": False}]

def test_credit_of_put_spread():
    q = {"SPY261127P00650000": (0.90, 1.00), "SPY261127P00640000": (0.40, 0.50)}
    assert b.credit_of(LEGS, q) == (0.50, 0.40)

def test_build_order_signs_and_intents():
    o = b.build_order(LEGS, 3, 0.50, opening=True)
    assert o.limit_price == -0.50 and o.qty == 3 and len(o.legs) == 2
    assert o.legs[0].side.value == "sell" and o.legs[1].side.value == "buy"
    c = b.build_order(LEGS, 3, 0.25, opening=False)
    assert c.limit_price == 0.25 and c.legs[0].side.value == "buy"

def test_plan_skips_on_unusable_quote(monkeypatch):
    def boom(*a, **k): raise RuntimeError("no usable quote")
    monkeypatch.setattr(b, "leg_quotes", boom)
    monkeypatch.setattr(b, "spy_last", lambda s: 672.40)
    monkeypatch.setattr(b, "trading_days", lambda t, d: WEEK)
    assert b.plan_nectar(None, None, None, D(2026, 11, 16)) is None

def test_plan_skips_when_credit_too_rich(monkeypatch):
    # mid credit 3.24 on a 5$ wing = 65% > 60% -> bad quotes
    monkeypatch.setattr(b, "spy_last", lambda s: 672.40)
    monkeypatch.setattr(b, "trading_days", lambda t, d: WEEK)
    monkeypatch.setattr(b, "leg_quotes", lambda opt, syms, **k: {
        "short_put": (3.40, 3.50), "long_put": (0.20, 0.30),
        "short_call": (0.05, 0.07), "long_call": (0.01, 0.03)})
    assert b.plan_nectar(None, None, None, D(2026, 11, 16)) is None
```

(`WEEK`, `D` wie in Task 1 in die Testdatei übernehmen. `leg_quotes` wird in `plan_nectar` mit einem Dict Beinname → OCC-Symbol aufgerufen und liefert Beinname → (Bid, Ask), wie `condor_paper.leg_quotes`; `credit_of` bekommt die Kurse nach Symbol umgeschlüsselt.)

- [ ] **Step 2: Fehlschlag sehen**

Run: `python -m pytest tests/test_spread_bot.py -v`
Expected: neue Tests FAIL

- [ ] **Step 3: Implementieren**

- `trading_days(trading, day) -> set[date]`: Alpaca-Kalender von `day` bis `day + 21 Tage`.
- Nectar: Wenn die Donnerstags-Strikes nicht quotierbar sind, einmal mit dem Freitag derselben Woche versuchen, dann aufgeben.
- Comb: Deltas über `OptionHistoricalDataClient.get_option_chain(OptionChainRequest(underlying_symbol="SPY", expiration_date=expiry, type="put"))`; nur Strikes mit vorhandenem Delta an `pick_delta_strike` geben. Kein Delta oder Long Put nicht quotierbar → `None`.
- Beide: `None`, wenn Mid-Credit ≤ 0 oder > `SPREAD_MAX_CREDIT_FRAC × width`. Bei Nectar ist `width` = 5.
- `enter`: Start am Mid, bis `SPREAD_MAX_CONCEDE` Cent nachgeben. `credit` der Position = tatsächlicher Fill (`filled_avg_price`, Betrag). `buyback` = `buyback_price(credit, SPREAD_BUYBACK[strategy])`. Ohne `submit` nur loggen und `None` liefern.
- Jeder Grund fürs Aussetzen wird geloggt und per `notify` gemeldet.

- [ ] **Step 4: Tests laufen lassen**

Run: `python -m pytest tests -v`
Expected: alle PASS

- [ ] **Step 5: Commit**

```bash
git add spread_bot.py tests/test_spread_bot.py
git commit -m "feat: spread bot entries (Nectar condor, Comb 15-delta put spread)"
```

---

### Task 5: Ausstieg und Abgleich

**Files:**
- Modify: `spread_bot.py`
- Test: `tests/test_spread_bot.py`

**Interfaces:**
- Consumes: Position und Zustand aus Task 3, `build_order`/`work`/`credit_of` aus Task 4
- Produces:
  - `reconcile(trading, state: dict) -> dict` — gleicht Zustand mit Konto ab (siehe Tests)
  - `ensure_buyback(trading, pos: dict, submit: bool) -> None` — legt die Tages-Limit-Order zum `buyback`-Preis ein, wenn keine offene existiert; speichert `tp_order_id`
  - `close_position(opt, trading, pos: dict) -> Order | None` — Rückkauf-Order stornieren, dann in zwei Durchgängen schließen wie `condor_paper.close_condor` (ab Mid in 1-Cent-Schritten, dann ab Natural in 5-Cent-Schritten, Deckel = `width`)
  - `record_exit(pos: dict, reason: str, debit: float | None, trading) -> None` — CSV-Zeile, Discord-Meldung; `reason` ∈ `"buyback"`, `"expiry_close"`, `"gone"`

- [ ] **Step 1: Tests schreiben**

```python
class FakeTrading:
    def __init__(self, positions=(), orders=()):
        self._p, self._o = list(positions), list(orders)
    def get_all_positions(self): return self._p
    def get_orders(self, *a, **k): return self._o

class P:  # position stub
    def __init__(self, symbol, qty): self.symbol, self.qty = symbol, str(qty)

POS = {"strategy": "comb", "entry_date": "2026-11-16", "expiry": "2026-11-27", "legs": LEGS,
       "qty": 2, "width": 10.0, "mid_credit": 0.52, "credit": 0.50, "buyback": 0.25,
       "tp_order_id": None, "equity_before": 5000.0}

def test_reconcile_keeps_position_that_is_still_there(monkeypatch):
    st = {"nectar": None, "comb": dict(POS), "skipped": {}, "halted": None}
    t = FakeTrading([P(LEGS[0]["symbol"], -2), P(LEGS[1]["symbol"], 2)])
    assert b.reconcile(t, st)["comb"] is not None

def test_reconcile_marks_vanished_position_as_closed(monkeypatch):
    seen = []
    monkeypatch.setattr(b, "record_exit", lambda pos, reason, debit, trading: seen.append(reason))
    st = {"nectar": None, "comb": dict(POS), "skipped": {}, "halted": None}
    out = b.reconcile(FakeTrading([]), st)
    assert out["comb"] is None and seen == ["buyback"]

def test_reconcile_halts_on_spy_shares(monkeypatch):
    monkeypatch.setattr(b, "notify", lambda *_: None)
    st = {"nectar": None, "comb": dict(POS), "skipped": {}, "halted": None}
    out = b.reconcile(FakeTrading([P("SPY", 200)]), st)
    assert out["halted"] == "assignment"

def test_reconcile_halts_on_partial_legs(monkeypatch):
    monkeypatch.setattr(b, "notify", lambda *_: None)
    st = {"nectar": None, "comb": dict(POS), "skipped": {}, "halted": None}
    out = b.reconcile(FakeTrading([P(LEGS[1]["symbol"], 2)]), st)   # short leg missing
    assert out["halted"] == "legs_mismatch"
```

- [ ] **Step 2: Fehlschlag sehen**

Run: `python -m pytest tests/test_spread_bot.py -v`
Expected: neue Tests FAIL

- [ ] **Step 3: Implementieren**

- Verschwundene Position: Grund ist `"buyback"`, wenn die gespeicherte `tp_order_id` gefüllt ist oder nicht mehr abfragbar; der Debit kommt aus der Order, sonst `None`.
- `halted` gesetzt heißt: keine neuen Einstiege, bestehende Rückkauf-Orders und der Verfallstag-Ausstieg laufen weiter. Meldung per Discord einmal pro Tag. Zurücksetzen nur von Hand (`--clear-halt`).
- CSV-Spalten: `strategy, entry_date, expiry, qty, width, mid_credit, credit, buyback, exit_date, exit_reason, exit_debit, pnl, equity_before, equity_after`. `pnl = (credit - exit_debit) * 100 * qty`.
- Scheitert `close_position` am Verfallstag: laute Discord-Meldung, alle 60 Sekunden neu versuchen bis Börsenschluss.

- [ ] **Step 4: Tests laufen lassen**

Run: `python -m pytest tests -v`
Expected: alle PASS

- [ ] **Step 5: Commit**

```bash
git add spread_bot.py tests/test_spread_bot.py
git commit -m "feat: spread bot exits (daily buy-back order, expiry close, reconcile)"
```

---

### Task 6: Tagesschleife, Starter, Doku

**Files:**
- Modify: `spread_bot.py`, `README.md`
- Create: `run_spreads.bat`
- Test: `tests/test_spread_bot.py`

**Interfaces:**
- Consumes: alles Vorherige
- Produces:
  - `capital_and_free(trading, state) -> tuple[float, float]` — Kapital K = Cash − Σ(credit × 100 × qty) der offenen Positionen; frei = min(Cash − Σ(width × 100 × qty), `options_buying_power`)
  - `tick(now: datetime, stock, opt, trading, state: dict, submit: bool) -> dict` — ein Durchlauf, liefert den neuen Zustand

- [ ] **Step 1: Tests schreiben**

```python
class Acct:
    def __init__(self, cash, obp): self.cash, self.options_buying_power, self.equity = str(cash), str(obp), str(cash)

def test_capital_and_free_with_open_comb():
    t = FakeTrading(); t.get_account = lambda: Acct(5100, 3100)
    st = {"nectar": None, "comb": dict(POS), "skipped": {}, "halted": None}
    # credits 0.50*100*2 = 100 -> K = 5000 ; reserved 10*100*2 = 2000 -> free = min(3100, 3100)
    assert b.capital_and_free(t, st) == (5000.0, 3100.0)

def test_capital_and_free_capped_by_options_buying_power():
    t = FakeTrading(); t.get_account = lambda: Acct(5000, 1200)
    st = {"nectar": None, "comb": None, "skipped": {}, "halted": None}
    assert b.capital_and_free(t, st) == (5000.0, 1200.0)
```

- [ ] **Step 2: Fehlschlag sehen**

Run: `python -m pytest tests/test_spread_bot.py -v`
Expected: neue Tests FAIL

- [ ] **Step 3: `tick` und Schleife implementieren**

Reihenfolge je Durchlauf (alle 30 Sekunden, nur an Handelstagen zwischen Öffnung und Schluss):

1. `reconcile`.
2. Für jede offene Position: ist heute ihr Verfallstag und `now >= expiry_close_time(session_close)` → `close_position` + `record_exit("expiry_close")`. Sonst `ensure_buyback`.
3. Einstiege, wenn nicht `halted`, `SPREAD_ENTRY_TIME <= now < SPREAD_ENTRY_END` und die Strategie für diese Woche weder offen noch in `skipped` vermerkt ist:
   - Comb nur montags.
   - Nectar montags; dienstags, wenn der Montag kein Handelstag war oder Nectar am Montag ausgesetzt hat (`skipped["nectar"]` trägt das Montagsdatum; Dienstag ist der letzte Versuch).
   - Sind beide heute fällig und beide frei: beide planen, `allocate(K, loss, SPREAD_SHARES)`, dann Comb vor Nectar einsteigen.
   - Ist die andere Strategie offen: `qty_alongside(K, free, share, loss)`.
   - Vor jeder Order: `qty * (width - limit_credit) * 100 <= free` erneut prüfen, sonst `qty` senken. (Maximalverlust, nicht die volle Breite: der Credit fließt mit dem Fill ins Cash. Bei 871 $ ergibt das 2 Kontrakte Nectar wie im Spec.)
   - Ergebnis 0 oder kein Fill → in `skipped` mit Datum vermerken, melden.
4. Zustand speichern.

Ausnahmen in der Schleife: loggen, Discord-Meldung, 120 Sekunden warten (wie `trend_bot.py`).
`run_spreads.bat`: Kopie von `run_trend.bat` mit `python spread_bot.py`.
README: kurzer Abschnitt mit Regeln, Größenformel, Befehlen (`--now`, `--submit`, `--status`, `--clear-halt`) und dem Hinweis, dass die Regeln ungetestet sind.

- [ ] **Step 4: Tests und Trockenlauf gegen das Paper-Konto**

Run: `python -m pytest tests -v` → alle PASS
Run (PowerShell): `$env:BOT_STRATEGY = 'spreads'; python spread_bot.py --now`
Expected: gibt für Nectar und Comb Strikes, Verfall, Mid-Credit, Kapital, freies Bargeld und Kontraktzahl aus und sendet keine Order. Am Wochenende: Meldung "market closed" ohne Fehler. (Solange UPRO im Konto liegt, ist das freie Bargeld klein und die Kontraktzahl 0 — das ist erwartet.)

- [ ] **Step 5: Commit**

```bash
git add spread_bot.py run_spreads.bat README.md tests/test_spread_bot.py
git commit -m "feat: spread bot daily loop, launcher and README"
```

---

### Task 7: Ablösung des Trend-Bots (nur mit ausdrücklicher Bestätigung des Nutzers)

Kein Code. Jeder Schritt greift in das Paper-Konto oder den Autostart ein und wird einzeln bestätigt.

- [ ] **Step 1:** Laufenden `trend_bot.py`-Prozess beenden (`run_trend.bat`-Fenster schließen).
- [ ] **Step 2:** UPRO/SGOV-Bestand anzeigen, nach Bestätigung zum Markt verkaufen (nur während der Handelszeit).
- [ ] **Step 3:** In `config.py` den Standardwert `STRATEGY` auf `"spreads"` stellen.
- [ ] **Step 4:** `AlpacaTrendBot.cmd` im Startup-Ordner auf `run_spreads.bat` umstellen.
- [ ] **Step 5:** `python spread_bot.py --status` → zeigt Cash ≈ Kontostand, keine Positionen. Dann `run_spreads.bat` starten.
- [ ] **Step 6:** Commit der `config.py`-Änderung; Memory `project-trend-bot` aktualisieren (Trend-Bot abgelöst).
