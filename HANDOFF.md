# AlphaGrid — session handoff

> Read this first if you're picking the project up cold. It captures what's
> built, what's not, what the locked decisions are, and where to look next.
> Last updated: 2026-05-19.

## TL;DR

AlphaGrid is a feature-complete 8-agent algorithmic trading system covering
Indian equities (Angel One) and crypto (Binance). All agents are real, the
real-time layer is wired, the dashboard is live, CI is green, and a
backtest harness validates strategies against historical data. **207 tests
pass.** The system is ready to enter a 7-day paper burn-in. Real capital
is gated behind 4 locked paper-to-live triggers.

The user is **Shresth Samyak**, a solo builder in India deploying his own
capital (₹5K–₹20K). He optimizes for an auditable track record, not
short-term P&L. He has paid Azure credits and intends to host on a single
`Standard_B2s` VM in South India.

---

## Where we are in the build plan

| Phase | Status |
|---|---|
| Week 1 — infra (Docker, SQLite, Redis, paper toggle) | done |
| Week 2 — track_record.py (append-only log) | done — 9 tests |
| Week 3 — Telegram approval gate | done — 12 tests |
| Week 4 — research agents (India + crypto) | done — 8 tests |
| Week 5 — trading_funding (carry arb) | done — 14 tests |
| Week 6 — trading_momentum (EWMA + ATR + ADX) | done — 13 tests |
| Week 7 — trading_sentiment (decay-weighted FinBERT) | done — 15 tests |
| Week 8 — trading_pairs / trend / crypto_sent gate | done — 21 tests |
| Week 9 — Real-time layer (SignalBus, WebSocket, NewsPoller) | done — 22 tests |
| Week 9 — Dashboard (Next.js + FastAPI replaced Streamlit) | done — 9 API tests |
| **Week 10 — 7-day paper burn-in** | **not started — operator-only** |
| Week 11 — flip live with ₹5K | gated |

Operator tools built but not on the original build plan: `healthcheck`,
`kill_switch_demo`, `daily_snapshot`, `weekly_report`, `backtest`.
Deployment: `setup.sh` + `deploy.sh` + 4 systemd units + Nginx. CI: 3
GitHub Actions workflows including a gated auto-deploy to Azure.

---

## Architecture map

```
hedgefund/
├── agents/                       # 8 real agents + base.py + news_poller.py
│   ├── base.py                   # Agent + AgentCadence abstract
│   ├── research_india.py         # 15min: news + sentiment + last_close
│   ├── research_crypto.py        # 8h: funding rates + regime
│   ├── trading_funding.py        # 8h: BTC/ETH carry; 7 refinement rules
│   ├── trading_momentum.py       # 5min IST session: EWMA cross, ATR, ADX, vol
│   ├── trading_sentiment.py      # 15min IST: decay-weighted FinBERT
│   ├── trading_pairs.py          # 30min: cointegration + Z-score
│   ├── trading_trend.py          # 1h: multi-speed EWMA on BTC/ETH/SOL
│   ├── trading_crypto_sent.py    # 4h: regime gate (no trades; writes modifier)
│   └── news_poller.py            # 30s news-speed daemon
├── api/main.py                   # FastAPI — REST + WS /live broadcaster
├── backtest/                     # historical replay harness
│   ├── clock.py                  # VirtualClock
│   ├── historical_feeds.py       # no-future-leakage feeds
│   └── runner.py                 # orchestration
├── comms/                        # approval_gate.py + telegram_bot.py
├── config/settings.py            # SINGLE source for thresholds; do NOT hardcode
├── data/                         # feeds_india + feeds_crypto + live_crypto_stream
├── deploy/                       # Azure: setup.sh, deploy.sh, systemd/, nginx/
├── dashboard/                    # empty — Streamlit deleted, dashboard lives in web/
├── execution/trade_router.py     # proposal -> risk -> approval -> log
├── infra/signal_bus.py           # InMemoryBus (default) + RedisBus
├── models/                       # indicators, candle_builder, finbert_scorer, pairs
├── record/                       # track_record.py + research_log.py (both append-only)
├── risk/risk_manager.py          # 6 locked rules; the trust boundary
├── tests/                        # 207 tests across 14 files
├── tools/                        # operator CLIs (5 of them)
├── web/                          # Next.js 16 dashboard (was Next 14; user upgraded)
├── .github/workflows/            # python-ci, web-ci, deploy
├── main.py                       # entry: AppContext + scheduler + WS + news_poller
├── HANDOFF.md                    # this file
├── README.md                     # public-facing docs
├── requirements.txt              # production deps
├── test-requirements.txt         # CI-only lightweight subset
└── pyproject.toml                # ruff + pytest config
```

---

## The 8 agents

All real. All tested. All read thresholds from `config.settings.strategy`.

| Agent | Cadence | Reads | Writes | Refinements memory |
|---|---|---|---|---|
| `research_india` | 15m | GoogleNews RSS + yfinance | `research_log: sentiment_score, last_close` | n/a |
| `research_crypto` | 8h (aligned to Binance funding) | Binance funding API | `research_log: funding_rate, regime` | n/a |
| `trading_funding` | 8h | research_log funding | `track_record` carry trades | `project_funding_arb_refinements.md` |
| `trading_momentum` | 5m IST session | yfinance OHLC | `track_record` long swings | `project_momentum_refinements.md` |
| `trading_sentiment` | 15m IST | research_log sentiment + yfinance OHLC | `track_record` small longs | `project_sentiment_refinements.md` |
| `trading_pairs` | 30m IST | yfinance OHLC for both legs | `track_record` two-leg orders | (rules in code/settings) |
| `trading_trend` | 1h | Binance OHLC | `track_record` directional | (rules in code/settings) |
| `trading_crypto_sent` | 4h | research_log regime + MVRV + social | `research_log: crypto_size_modifier` | (writes only; modulates funding+trend) |

---

## The 4-speed runtime

```
Tick speed   wss://stream.binance.com (spot, region-tolerant)
                  -> CandleBuilder -> bus on `price.<symbol>`     ~ms

News speed   Google News RSS poller -> FinBERT (or NullScorer)
                  -> research_log + bus on `news.alert`           2-32s

Bar speed    APScheduler -> 8 agents on their cadence             5m/15m/30m/1h/4h/8h
                  -> trade_router -> risk_manager -> track_record

Macro speed  research_crypto (8h) + trading_crypto_sent (4h)      hours
                  -> regime + crypto_size_modifier
```

All four share one `InMemoryBus` in single-process mode; auto-switches
to `RedisBus` when the api process is run separately on Azure.

`AppContext` in `main.py` is the single boot-time container. Construct it,
start the WS stream + news poller, then start the scheduler.

---

## Locked decisions (do not relitigate)

These came directly from the user and are encoded in code, settings, or memory:

- **Funding-arb thresholds:** enter ≥ 0.0001 (0.01% per 8h), exit < 0.00005, 3 stable windows, 0.80 decay floor, 2x leverage cap. Fractions, not bps — last bug fix.
- **Momentum:** EWMA 8/32, 200-EMA trend gate, ADX > 20, 1.5× / 2× ATR stop+target, sentiment soft-gate. `momentum_min_history_bars=210` because 200-EMA needs 200+buffer to seed.
- **Sentiment:** entry 0.72 latest, decay-weighted average ≥ 0.72, min 3 headlines, panic exit ≤ -0.30, max 5d hold.
- **Pairs:** Engle-Granger p<0.05, OU half-life ≤ 10d, Z entry |z|≥2 / exit |z|≤0.5 / stop |z|≥3, weekly refit.
- **Trend:** 2-of-3 multi-speed agreement, 10% vol target, 3× max leverage.
- **Risk manager:** 2% per trade hard cap, 10% drawdown over 30d kill switch, half-Kelly sizing.
- **Drawdown semantics:** max-drawdown over rolling window. **Kill switch persists even after equity recovery** until the loss event ages out (intentional — see `tests/test_kill_switch_integration.py::test_kill_switch_persists_after_equity_recovery`).
- **Streamlit deleted.** User explicitly does not want it. Dashboard is Next.js (in `web/`) + FastAPI (in `api/`).
- **Spot endpoint for Binance WebSocket.** Futures was region-blocked. `futures=True` is opt-in.
- **No real money before 4 triggers:** 60 trading days + Sharpe ≥ 0.8 + paper-vs-backtest gap < 20% + 30 clean days. See `project_paper_to_live_triggers.md`.
- **First live capital is ₹5K, not ₹20K.** 30-day half-cap sizing before scaling.

---

## Test inventory (207 passing in ~16s)

| File | Tests | Covers |
|---|---|---|
| `test_track_record.py` | 9 | append-only guarantee, PnL math, agent_stats, drawdown |
| `test_research_log.py` | 7 | write, recent window, batch, finite-value guard |
| `test_risk_manager.py` | 11 | each rule in isolation, kill switch boundary |
| `test_approval_and_router.py` | 12 | NullApprovalGate, TelegramApprovalGate, TradeRouter |
| `test_pdf_report.py` | 2 | reportlab month rendering |
| `test_research_agents.py` | 8 | research_india + research_crypto |
| `test_trading_funding.py` | 14 | all 7 refinement rules |
| `test_trading_momentum.py` | 13 | each filter, stop/target exits |
| `test_trading_sentiment.py` | 15 | decay weighting, panic exit |
| `test_pairs_trend_regime.py` | 21 | cointegration, OU math, trend votes, regime gate |
| `test_indicators.py` | 12 | EWMA / ATR / ADX / detect_cross |
| `test_realtime_layer.py` | 14 | SignalBus, CandleBuilder, NewsPoller |
| `test_live_crypto_stream.py` | 10 | spot/futures URL, tick handler |
| `test_healthcheck.py` | 12 | run_check, HealthReport, offline path |
| `test_daily_snapshot.py` | 10 | aggregation, JSONL append, kill-switch flag |
| `test_kill_switch_integration.py` | 5 | end-to-end through TradeRouter |
| `test_weekly_report.py` | 12 | 6 metrics + render + thresholds |
| `test_api.py` | 9 | REST endpoints + WS fan-out |
| `test_backtest.py` | 11 | clock, feeds, runner, **no-future-leakage invariant** |

---

## Operator tooling

| CLI | Purpose | Exit-code semantics |
|---|---|---|
| `python -m tools.healthcheck` | 8 pre-burn-in checks; pulls real services | 0 iff every essential check passes |
| `python -m tools.healthcheck --offline` | Skip network checks (CI / local) | same |
| `python -m tools.kill_switch_demo` | Demonstrate the 10% drawdown safety net | 0 iff assertions hold |
| `python -m tools.daily_snapshot` | Append last-24h per-agent JSON line + write text summary | 0 always |
| `python -m tools.weekly_report` | The 6 paper-trading metrics with PASS/FAIL | 0 iff every metric passes |
| `python -m tools.backtest --days N` | Replay real Binance+yfinance data through every agent | 0 iff report.overall_pass |
| `python -m tools.backtest --offline` | Same but with synthetic deterministic data | same |

Outputs all go to `reports/` (in `.gitignore`).

---

## CI / CD

Three GitHub Actions workflows in `.github/workflows/`:

| Workflow | Trigger | Does |
|---|---|---|
| `python-ci.yml` | push/PR touching `*.py`, `test-requirements.txt`, `pyproject.toml` | `ruff check` + `pytest --timeout=30` against `test-requirements.txt` |
| `web-ci.yml` | push/PR touching `web/` | Node 20 → `npm install` → `typecheck` → `build` |
| `deploy.yml` | python-ci green on main, or manual dispatch | SSH into Azure → `git pull` → `pip install` → `systemctl restart` |

`deploy.yml` skips silently if `AZURE_HOST` / `AZURE_SSH_KEY` repo secrets
aren't set — safe to merge before VM exists.

---

## Azure deployment (single-VM)

```
Browser → Vercel (Next.js, free)
            ↓ REST + WebSocket
          Azure VM (Standard_B2s, ~$30/mo)
            └─ nginx :80
                ├─ uvicorn :8000  (api.main:app)         REST + WS broadcast
                └─ python -m main                         scheduler + 8 agents
                            ↕ Redis 127.0.0.1
                          PostgreSQL (trade log + research log)
```

Bootstrap on a fresh Ubuntu 22.04 VM:

```bash
sudo apt install -y git
git clone https://github.com/ShresthSamyak/hedgefund.git /tmp/hedgefund
sudo bash /tmp/hedgefund/deploy/setup.sh         # idempotent
sudo -u alphagrid nano /home/alphagrid/hedgefund/.env   # add API keys
sudo systemctl restart alphagrid alphagrid-api
```

Subsequent deploys from operator's laptop:

```bash
export AZURE_HOST=azureuser@20.235.xxx.xxx
./deploy/deploy.sh "commit message"
```

Frontend lives on Vercel — point `NEXT_PUBLIC_API_URL` and `NEXT_PUBLIC_WS_URL`
at the VM's public IP. Every git push redeploys.

---

## Codebase invariants (enforced by tests — preserve under refactor)

1. **Track record is append-only.** Closed rows raise `TrackRecordImmutableError` on mutation. `test_track_record.py::test_closing_twice_raises` + SQLAlchemy event guards in `record/track_record.py`.
2. **Research log values are finite.** NaN/Inf rejected at `ResearchLog.write` boundary. `_ensure_finite()` in `record/research_log.py`. Bug history: yfinance emits NaN near market open — caught during the backtest run.
3. **Kill switch persists within the 30d window.** Equity recovery does NOT release it. `test_kill_switch_integration.py::test_kill_switch_persists_after_equity_recovery`.
4. **No future leakage in backtests.** `HistoricalIndiaFeed` / `HistoricalCryptoFeed` reveal only `ts <= clock.now()`. `test_backtest.py::test_no_future_leakage_invariant` spies on every `fetch_ohlc`.
5. **Risk manager is the trust boundary.** Every trade through `TradeRouter` passes through `RiskManager.review()`. No agent talks to `TrackRecord.open_trade()` directly.
6. **InMemoryBus dispatch is on a daemon thread.** Publishers don't block. `infra/signal_bus.py`.
7. **Strict JSON in research_log payload.** `_ensure_json_safe()` uses `json.dumps(payload)` with no `default=` fallback.

---

## Bugs caught and fixed during the build

These are now permanent regression tests — don't reintroduce:

| Bug | Where surfaced | Fix |
|---|---|---|
| `funding_enter_bps=0.01` compared against ccxt fractions (100× mismatch) | tier-sizing test | Renamed to `_rate`, set to `0.0001` |
| FinBERT pipeline returned tensors; `cast` needed | Pyright | Added `cast(list[list[dict[str, Any]]], pipe(texts))` |
| SQLite drops tzinfo → naive vs aware datetime subtract | sentiment max-holding | `_aware()` helper in trading_sentiment, daily_snapshot, weekly_report, backtest |
| Streamlit `from config.settings import` failed when CWD = `dashboard/` | the screenshot user shared | Deleted Streamlit; built Next.js+FastAPI |
| Binance futures stream silently delivers no messages from India | first live WS test | Default to spot endpoint; `futures=True` opt-in |
| `BinanceWebSocketStream.stop()` blocked on websocket recv | shutdown timeout | Cancel task instead of await; fire-and-forget |
| FastAPI 0.109 incompatible with Starlette 1.0 | API test collection | Upgraded fastapi to 0.136.1 |
| `:memory:` SQLite + FastAPI threadpool → each thread fresh DB | API tests | Use `tmp_path / 'test.db'` |
| OU half-life returns huge finite value when β slightly negative | test_ou_half_life_on_random_walk | Test accepts None OR huge value |
| Trend downtrend test produced negative close prices | test runner crash | `max(50, ...)` floor in synthetic data |
| yfinance NaN close near market open | live backtest IntegrityError | `_ensure_finite` guard + tools/backtest filter |
| Mathematical redundancy: "all ≥ threshold" + "decay-weighted ≥ threshold" | sentiment decay test | Restructured to "latest ≥ threshold" + "decay-weighted ≥ threshold" |

---

## Memory pointers

Structured memories under `C:\Users\HP\.claude\projects\C--Users-HP-Documents-hedgefund\memory\`:

- `MEMORY.md` — index, loaded on every session
- `project_alphagrid.md` — system architecture
- `user_profile.md` — solo builder, ₹5K-20K, auditable track record
- `project_tech_stack.md` — locked tooling
- `project_build_order.md` — week-by-week sequencing
- `project_funding_arb_refinements.md` — 7 carry-trade rules with citations
- `project_momentum_refinements.md` — EMA crossover filters with citations
- `project_sentiment_refinements.md` — decay-weighted FinBERT rules
- `project_paper_to_live_triggers.md` — 4 locked criteria

Refer to these before suggesting changes to settings or strategy logic.

---

## What's next

Operator-side, not code-side. The 7-day paper burn-in is the next deliverable:

1. (Optional code) Telegram daily digest of `daily_snapshot` output — currently the user has to SSH in to check
2. **Run the burn-in.** `python main.py` + dashboard for 7 days. Then 30 days. Then 60 days.
3. Cron `daily_snapshot` and `weekly_report` via the systemd timers already configured.
4. After 60 trading days, check the 4 paper-to-live triggers via `weekly_report` + `backtest` JSONL comparison.
5. If all four trip → flip `PAPER_MODE=false`, fund ₹5K, half-cap sizing for 30 days.

There's nothing else in the original architecture doc that's unbuilt.
Future enhancements the user has mentioned but not committed to:

- Glassnode integration (paid $39/mo) → would feed MVRV into `trading_crypto_sent`
- Reddit/X sentiment → social_sentiment input for the regime gate
- LLM reasoning summaries on each trade for the dashboard

---

## How to resume in a new session

1. **Read this file first.** It's load-bearing.
2. Check `MEMORY.md` for the 9 structured memories.
3. Run `python -m pytest tests/ -q --timeout=20` to confirm 207 passing.
4. Run `python -m tools.healthcheck --offline` to confirm essentials green.
5. If the user references something specific, grep first — don't guess:
   - settings: `config/settings.py`
   - any agent: `agents/<name>.py`
   - operator CLIs: `tools/`
6. **Do not** suggest deleting tests, hardcoding thresholds, or weakening the kill switch / append-only / no-future-leakage invariants.
7. **Do not** suggest re-adding Streamlit.
8. **Do not** flip `PAPER_MODE=false` without the 4 triggers passing.

When in doubt about what's already built: `git log --oneline -50`.
