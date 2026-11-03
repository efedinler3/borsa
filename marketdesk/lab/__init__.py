"""Test laboratuvarı: 5-year historical data, four rule-based strategy bots, a referee ("hakem") that learns which
bot to trust, a walk-forward test, and the live paper-trading hook.

- store.py    lab.db (separate SQLite file next to marketdesk.db): universe, daily bars, earnings, SEC filings,
              Finnhub headlines, download progress, run reports, the trained model (JSON, no pickle)
- download.py resumable background download / daily incremental update (Yahoo, Nasdaq, SEC EDGAR, Finnhub)
- bots.py     features + the four bots on daily bars, outcomes of a same-day trade (open -> stop/target/close)
- hakem.py    weighted vote + logistic-regression referee, train on the first years, test on the last year
- runner.py   background job control (status/progress) used by the scheduler and the HTTP API
"""
