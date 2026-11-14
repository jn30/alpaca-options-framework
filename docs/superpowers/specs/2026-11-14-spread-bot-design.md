# Spread-Bot (Nectar + Comb) — Design

Stand: 2026-11-14. Status: freigegeben am 2026-11-14 (Comb-Verfall vom Nutzer geklärt).

## Absicht

Ein Bot handelt zwei feste SPY-Regelwerke ("Nectar" und "Comb") auf dem
Alpaca-Paper-Konto und wählt die Kontraktzahl aus dem vorhandenen Bargeld
statt aus festen Stückzahlen. Er löst den Trend-Bot auf diesem Konto ab.

## Ziele

- Beide Regelwerke exakt nach Vorgabe handeln (Strikes, Verfall, Rückkauf).
- Größe skaliert mit dem Konto: das gesamte Bargeld darf im Risiko stehen.
- Läuft unbeaufsichtigt, übersteht Neustarts, meldet jeden Schritt per Discord.

## Nicht-Ziele

- Kein Backtest. Die Regeln sind ungetestet; verwandte Tests im Projekt
  (30–45-DTE-Put-Spreads, 0DTE-Condor) waren negativ.
- Kein Echtgeld. Der Bot startet nur mit `config.PAPER = True`.
- Kein Stop-Loss, keine Filter, keine Parameter-Optimierung.
- Trend-Bot und Spread-Bot laufen nie gleichzeitig.

## Was auf keinen Fall passieren darf

- Der Maximalverlust aller offenen Spreads übersteigt das Bargeld.
- Margin wird genutzt oder ein Short-Bein steht ohne Long-Bein da.
- Eine zweite Position derselben Strategie wird eröffnet, solange eine offen ist.
- Eine Position wird über das Wochenende ihres Verfalls gehalten.

## Die Regelwerke

### Nectar (Iron Condor)

- Einstieg Montag; fällt Montag aus (Feiertag, kein Fill, kein Budget), Dienstag.
- Short Put = SPY − 2,50 $, auf ganzen Dollar gerundet, muss unter SPY liegen
  (sonst 1 $ tiefer). Long Put = Short Put − 5 $.
- Short Call = SPY × 1,03, auf ganzen Dollar gerundet, muss über SPY liegen.
  Long Call = Short Call + 3 $.
- Verfall: Donnerstag bei Montags-Einstieg, Freitag bei Dienstags-Einstieg.
  Sind die Strikes am Donnerstag nicht gelistet: Freitag derselben Woche.
- Eine Order über alle vier Beine, gegen Credit.
- Rückkauf bei ≤ 75 % des erhaltenen Credits (auf Cent abgerundet).
- Sonst am Verfallstag schließen.

### Comb (Put-Credit-Spread)

- Einstieg Montag zwischen 10:00 und 15:00 ET.
- Verfall: der nächste Freitag mit mindestens 7 Tagen Restlaufzeit (vom Nutzer
  entschieden). Ab Montag ist das der Freitag der Folgewoche (11 Tage).
  Folge: Am nächsten Montag ist die Position meist noch offen, Comb steigt
  dann nicht neu ein. Wurde sie vorher zurückgekauft, steigt Comb wieder ein.
- Short Put = der Put mit Delta am nächsten an −0,15. Long Put = 10 $ darunter.
- Eine Order über beide Beine, gegen Credit.
- Rückkauf bei ≤ 50 % des erhaltenen Credits.
- Sonst am Verfallsfreitag schließen.

## Größe (vom Nutzer entschieden: ganzes Depot, nur Cash)

- Basis ist `account.cash`, zusätzlich gedeckelt durch `options_buying_power`.
  Wertpapierbestände und Margin zählen nicht.
- Kapital K = Cash minus die Credits der offenen Positionen.
- Maximalverlust pro Kontrakt = (Flügelbreite − Credit) × 100. Bei Nectar zählt
  der breitere Flügel (5 $), weil nur eine Seite verlieren kann.
- Kontrakte = Budget ÷ Maximalverlust pro Kontrakt, abgerundet. Keine Obergrenze.
- Ergibt das 0, setzt die Strategie diese Woche aus und meldet es.

## Ansätze zur Aufteilung

1. **Originalverhältnis 7 % Nectar / 93 % Comb, ungenutzter Anteil geht an die
   andere Strategie.** Bleibt am nächsten an den Originalgrößen (ca. 700 $ zu
   9.500 $ Risiko) und ist vorhersehbar. **Gewählt** — als Empfehlung von
   Claude übernommen, der Nutzer hat nicht widersprochen.
2. Hälfte/Hälfte. Einfacher, gibt Nectar aber viel mehr Gewicht als im Original.
3. Wer zuerst kommt, nimmt alles. Nutzt das Konto am vollsten, aber die
   Einstiegsreihenfolge bestimmt die Gewichtung zufällig.

Ablauf am Montag: Der Bot holt für beide Strategien die Kurse und rechnet die
Kontrakte aus dem jeweiligen Anteil. Kommt eine Strategie auf 0, bekommt die
andere deren Anteil. Kommen beide auf 0, versucht erst Comb, dann Nectar das
ganze Kapital. Ist beim Einstieg einer Strategie die andere schon offen (Nectar
am Dienstag, oder Montag in einer Woche mit laufender Comb-Position), gilt:
eigener Anteil, höchstens aber das noch freie Bargeld (Cash minus Flügelbreite
× 100 × Kontrakte der offenen Position).

Beispiel bei 871 $: Nectar 2 Kontrakte, Comb 0. Bei 10.500 $: Nectar 2, Comb 10.

## Aufbau

- **`spread_bot.py`**, eigenständig nach dem Muster von `condor_paper.py`:
  reine Hilfsfunktionen (Strikes, Verfall, Größe, Rückkaufpreis) getrennt vom
  Broker-Code, damit sie ohne Konto testbar sind.
- **`config.py`**: `STRATEGY = "spreads"` und ein Block `SPREAD_*`
  (Anteile, Einstiegszeit, Rückkaufquoten, Schließzeit, `SPREAD_SUBMIT`).
- **`run_spreads.bat`** wie `run_trend.bat` (Neustart nach Absturz, Exit 2 = Stopp).
- **Tests** für die Hilfsfunktionen unter `tests/`.
- Zustand in `logs/spread_state.json`, Trades in `logs/spread_trades.csv`
  (eine Zeile pro Position, mit Mid-Credit, Fill-Credit, Kontrakten, Budget,
  Ausstiegsgrund und Kontostand vorher/nachher).

## Verhalten

- Trockenlauf als Standard für Einzelaufrufe (`--now`), echte Paper-Orders nur
  mit `--submit` bzw. `SPREAD_SUBMIT = True` im Dauerlauf.
- Start wird verweigert, wenn `STRATEGY != "spreads"` oder `PAPER` aus ist.
  Derselbe Sperr-Port wie der Trend-Bot, damit nie beide laufen.
- Einstieg: Limit-Order am Mid, schrittweise um wenige Cent nachgeben (wie
  `work_order` im Condor-Bot). Kein Fill bis 15:00 ET: aussetzen.
- Rückkauf: Alpaca erlaubt bei Optionen nur Tages-Orders. Der Bot legt die
  Rückkauf-Order deshalb an jedem Handelstag nach Eröffnung neu ein.
- Verfallstag: um 15:30 ET Rückkauf-Order stornieren und die Position aktiv
  schließen, also vor Alpacas automatischem Rückkauf um 15:45 ET.
- Nach einem Neustart liest der Bot den Zustand und gleicht ihn mit den
  tatsächlichen Positionen ab, bevor er etwas tut.
- Vorzeitige Ausübung eines Short Puts (SPY-Aktien tauchen im Konto auf):
  melden, nichts Neues eröffnen, auf den Nutzer warten.
- Discord-Meldung bei Einstieg, Rückkauf, Schließen, Aussetzen und Fehlern.

## Ablösung des Trend-Bots (vom Nutzer entschieden)

1. Trend-Bot-Prozess beenden.
2. UPRO bzw. SGOV auf dem Paper-Konto verkaufen (einmalig, nach Bestätigung).
3. `STRATEGY` auf `"spreads"` stellen.
4. Autostart im Startup-Ordner (`AlpacaTrendBot.cmd`) auf `run_spreads.bat` umstellen.

Der Paper-Test des Trend-Bots (seit 2026-11-04, Ziel ≥ 20 Handelstage) endet damit.

## Festlegungen von Claude (nicht vom Nutzer entschieden)

- Einstiegszeit 10:00 ET für beide Strategien. Nectar nennt keine Uhrzeit.
- Schließzeit am Verfallstag 15:30 ET.
- Fällt der Verfallstag auf einen Feiertag: der nächste Handelstag derselben
  Woche, sonst der letzte davor.
- Liefert Alpaca kein Delta oder ist der 10-$-Flügel nicht gelistet: Comb
  setzt aus und meldet es.
- Plausibilitätsgrenze: Mid-Credit über 60 % der Flügelbreite gilt als
  fehlerhafter Kurs, dann aussetzen (wie `CONDOR_MAX_CREDIT_FRAC`).

## Dem Umsetzer überlassen

- Genaue Schrittweite und Dauer beim Nachgeben der Limit-Orders.
- Aufbau der Zustandsdatei und Spaltennamen der CSV.
- Wie die Optionskette und Deltas abgefragt werden (Snapshot oder Chain).
