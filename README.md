# AlphaGrid

An 8-agent autonomous trading system covering Indian equities (Angel One SmartAPI)
and crypto (Binance). Every trade routes through a shared risk manager and an
append-only track record — the latter is the load-bearing artifact for raising
capital later.

## Status

Week 1 / 11 — scaffolding + the two foundation modules.

| Layer | State |
|---|---|
| `record/track_record.py` — append-only trade log | done (9 tests green) |
| `risk/risk_manager.py` — 6 risk rules | done (11 tests green) |
| 8 agent stubs in `agents/` | stubs only — `NotImplementedError` until their week |
| `main.py` — APScheduler harness | runs, logs `not implemented yet` for each tick |
| Telegram approval gate | week 3 |
| Funding-arb agent (first live signal) | week 5 |

Full architecture and weekly build order live in the project memory.

## Quick start

```powershell
# 1. Install deps (Python 3.11+)
pip install -r requirements.txt

# 2. Copy env template, fill in keys as you go
Copy-Item .env.example .env

# 3. Start Redis (signal bus)
docker compose up -d redis

# 4. Run the test suite — should be 20/20 green
python -m pytest tests/ -v

# 5. Start the scheduler (agents currently log "not implemented yet")
python main.py
```

Paper mode is **on by default** (`PAPER_MODE=true` in `.env.example`). Nothing
will place real orders until that flag flips, the relevant agent ships, and the
Telegram approval gate is satisfied.

## Layout

```
alphagrid/
├── agents/             # 8 trading + research agents (stubs for now)
├── risk/risk_manager.py    # the trust boundary — every trade passes through
├── record/track_record.py  # append-only SQLite log; migrate to Postgres at wk 11
├── execution/          # broker wrappers (Angel One, Binance) — week 5+
├── data/               # NSE/BSE + Binance + Glassnode feeds — week 4
├── models/             # FinBERT, cointegration helpers — week 4+
├── comms/              # Telegram bot + approval gate — week 3
├── dashboard/          # Streamlit live + track-record views — week 9
├── config/settings.py  # all thresholds, toggles, credentials
├── tests/              # smoke tests (in-memory SQLite, no mocks)
├── main.py             # APScheduler entrypoint
└── docker-compose.yml  # Redis only; Postgres deferred to week 11
```

## Storage

We start on **SQLite** (single `alphagrid.db` file, zero infra) and migrate to
**PostgreSQL** by swapping `ALPHAGRID_DB_URL` at week 11. The schema is portable.

The append-only guarantee is enforced at the application layer:
- closed rows (`exit_ts NOT NULL`) raise `TrackRecordImmutableError` on any mutation
- `DELETE` against the `trades` table is blocked by a SQLAlchemy event hook

## Risk rules

All locked. Override via `.env` only if you know why.

| Rule | Default |
|---|---|
| Max % per trade | 2% of portfolio |
| Kill-switch drawdown | 10% over rolling 30d |
| Kelly fraction | 0.5 (half-Kelly), capped at 25% |
| Correlated long cap | 3 longs with pairwise corr > 0.70 |
| Indian intraday window | 09:15 – 15:25 IST |
| Crypto regime override | `risk_off` blocks directional crypto agents; funding arb still allowed |

## Real-time layer (week 9)

Three speeds run in parallel — see `agents/news_poller.py`,
`data/live_crypto_stream.py`, and `infra/signal_bus.py`:

| Speed | What | Channel | Latency |
|---|---|---|---|
| Tick  | Binance WebSocket `aggTrade` -> `CandleBuilder` -> bus | `price.<symbol>` | ~ms |
| News  | RSS poller -> FinBERT -> bus on `|score| ≥ 0.7` | `news.alert`, `news.raw` | 2–32s |
| Bar   | Scheduled agents on APScheduler (5m / 15m / 30m / 1h / 4h / 8h) | direct calls | matches cadence |
| Macro | research_crypto on-chain / regime gate | `research.regime` | 4–8h |

The bus has two implementations — `InMemoryBus` for paper mode and tests
(default), `RedisBus` for multi-process production. Channels are the same.

## Dashboard

```powershell
streamlit run dashboard/app.py
```

Two views: **Live** (open positions, risk status, latest signals, regime)
and **Track record** (cumulative P&L, per-agent stats, full trade table).
Reads directly from the SQLite DB — no scheduler dependency.

## Tests

```powershell
python -m pytest tests/ -v
```

160 tests covering the trade log, risk manager, all 8 agents, the indicator
math, pairs cointegration / OU half-life, news poller, candle builder,
signal bus, the live Binance WebSocket tick handler, and the pre-burn-in
health check.

## Pre-burn-in health check

Run this once before committing to a multi-day continuous run:

```powershell
python -m tools.healthcheck            # full check (~30s, real network)
python -m tools.healthcheck --offline  # essentials only, no network
```

Verifies settings load, SQLite roundtrip, risk-manager rules, paper trade
through TradeRouter, FinBERT score, Google News RSS, news poller end-to-end,
and a live Binance WebSocket bar. Exit code 0 iff every essential check
passes.

## Next milestone

Week 10 — full paper-trading burn-in (all 8 agents + real-time layer +
dashboard running for 7 days) before flipping live-mode for week 11.