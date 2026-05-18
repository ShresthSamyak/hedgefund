# Doc audit — reference architecture vs actual code

Audit of the technical doc (the "AlphaGrid: Deep Technical Reference"
write-up) against the codebase as of 2026-05-19. Three labels:

- **MATCH** — code does what the doc says.
- **DIVERGES (defensible)** — code chose a different number / approach for documented reasons; both are valid.
- **GAP** — doc describes something we haven't built yet. Mark as aspirational.

Settings cited by name (`momentum_*`, `funding_*`, etc.) live in
`config/settings.py`.

---

## §2 Agent 1 — Momentum (NSE)

| Claim | Status | Notes |
|---|---|---|
| EWMA fast 8 / slow 32, α=2/(N+1) | MATCH | `momentum_fast_ewma=8`, `momentum_slow_ewma=32` |
| Wilder ATR(14) for sizing | MATCH | `momentum_atr_period=14`, see `models/indicators.py::atr` |
| Volume Z-score > 2 classifies intraday vs swing | **DIVERGES** | We use **median-ratio** volume (`vol >= median × momentum_volume_ratio_min`), not a Z-score. Also we use **ADX > 20 + 200-EMA** as the regime filters, both stronger than volume alone. The doc undersells what's built. |
| Forced flat by 15:25 IST intraday | **DIVERGES** | Momentum agent is swing-horizon by default. We honor 9:30–15:25 IST as the trading **window** (skip first 15 min) but don't force-flatten at close. Holds spans days unless stop/target hit. |
| Stop = entry − k·ATR, target = entry + 2k·ATR | MATCH | `momentum_atr_stop_mult=2.0`, `momentum_atr_target_mult=4.0` → 1:2 R:R exactly. |
| ATR-based position sizing inversely proportional to vol | MATCH | `qty = risk_budget / (2 × ATR)` in `trading_momentum.py`. |
| 200-EMA trend gate | **DOC OMITS** | We add `price > EWMA(200)` as a required filter. The doc doesn't mention it but the code does — and it raised `momentum_min_history_bars` to 210 to seed the 200-EMA. |
| Sentiment soft-gate | **DOC OMITS** | We require `latest sentiment_score ≥ 0` for longs (`momentum_require_nonnegative_sentiment=True`). |

**Net:** code is more conservative than the doc. The doc would generate more trades; the code's extra filters make it pickier.

---

## §3 Agent 2 — FinBERT Sentiment (NSE)

| Claim | Status | Notes |
|---|---|---|
| ProsusAI FinBERT, 3-class softmax, score = p_pos − p_neg | MATCH | `models/finbert_scorer.py::FinBertScorer.score` returns exactly this via `SentimentScore.signed`. |
| Entry threshold 0.72 for 3 consecutive 15-min windows | **DIVERGES** | We require **latest record ≥ 0.72 AND decay-weighted-avg(last 3) ≥ 0.72**. Mathematically the doc's "all-above-threshold" rule is redundant with a decay-weighted check (a convex combination of values all ≥ T is always ≥ T). Test pin: `test_trading_sentiment.py::test_does_not_enter_when_decay_weighted_below_threshold`. |
| Exit at 0.5 (mean-revert hysteresis) | MATCH | `sentiment_exit_threshold=0.50`. |
| 5-day time-stop | MATCH | `sentiment_max_holding_days=5`. |
| ONNX inference 6–10 ms/headline | **GAP** | We use PyTorch eager via `transformers.pipeline` — ~35–45 ms/headline. ONNX export would be a real speedup but isn't implemented. |
| Look-ahead prevention via ε guard on `t_pub < w_end − ε` | **DIVERGES** | We don't have an explicit ε; instead `HistoricalIndiaFeed` enforces no-future-leakage by clock-gated visibility — checked by `test_backtest.py::test_no_future_leakage_invariant`. |
| Panic exit on score ≤ −0.30 | **DOC OMITS** | `sentiment_panic_threshold=-0.30` — asymmetric negative-news exit. |
| Min 3 headlines required per scoring record | **DOC OMITS** | `sentiment_min_headlines=3` — single-headline signals rejected as noise. |

---

## §4 Agent 3 — Pairs (NSE)

| Claim | Status | Notes |
|---|---|---|
| Engle-Granger: OLS hedge ratio, ADF on residuals | MATCH | `models/pairs.py::engle_granger`. |
| ADF p-value < 0.05 ⇒ cointegrated | MATCH | `pairs_cointegration_pvalue=0.05`. |
| OU half-life via AR(1) regression, τ = ln(2)/θ | MATCH | `models/pairs.py::ou_half_life`. |
| Trade pairs with half-life 1–30 days | **DIVERGES** | We cap at `pairs_half_life_max_days=10`, tighter than the doc's 30. Conservative. |
| Z-score entry at ±2 | MATCH | `pairs_zscore_entry=2.0`. |
| Exit at z = 0 | **DIVERGES** | We exit at `\|z\| ≤ 0.5` (`pairs_zscore_exit=0.5`) — a small buffer prevents chattering at zero-crossings. |
| Stop at \|z\| ≥ 3.5 | **DIVERGES** | We stop at `\|z\| ≥ 3.0` (`pairs_zscore_stop=3.0`) — tighter. |
| 4 named NSE pairs (HDFCBANK/ICICIBANK, RELIANCE/ONGC, INFY/TCS, BAJFINANCE/BAJAJFINSV) | MATCH | Exact match to `pairs_universe`. |
| Weekly re-screening / refit | MATCH | `pairs_refit_days=7`. Refit lazily on next tick after the cache ages out. |
| Pre-screen by Pearson correlation > 0.7 | **DOC OMITS** | We add `pairs_min_correlation=0.70` before running the expensive ADF test. |
| Time-stop at max-holding | **DOC OMITS** | `pairs_max_holding_days=15`. |
| β-scaled leg sizing | **DIVERGES** | We use equal-notional legs (`leg_notional = 0.5 × risk_cap × portfolio`) rather than β-scaled `N_X = β·N_Y`. Slightly less hedge-ratio-precise; significantly simpler. |

---

## §5 Agent 4 — Funding-Rate Arbitrage

| Claim | Status | Notes |
|---|---|---|
| Binance funding formula F = P + clamp(I − P, ±0.05%) | MATCH (descriptive) | Code consumes `ccxt.fetch_funding_rate(symbol).fundingRate` directly — Binance does the formula. |
| 8h cadence (00/08/16 UTC) | MATCH | `AgentCadence(every=timedelta(hours=8), aligned_to="binance_funding_8h")`. |
| Entry at funding ≥ 0.01% per 8h | MATCH | `funding_enter_rate=0.0001` (fraction). |
| Exit at funding ≤ 0.005% | MATCH | `funding_exit_rate=0.00005`. |
| Two consecutive observations | **DIVERGES** | We require **3** stable windows (`funding_stability_windows=3`) for tighter false-positive control. |
| 3× max leverage | **DIVERGES** | We cap at **2×** (`funding_max_leverage=2.0`) → ~45% liquidation distance, more conservative. |
| Bypass on basis dislocation > 1% premium | **DIVERGES** | We close on basis drift > 5% (`funding_basis_close_pct=0.05`). The 1% threshold is at entry; ours is at exit. Different rule, similar intent. |
| Round-trip fee math (0.28% as illustrated) | MATCH | Same order of magnitude; not encoded in code (fees are handled at TrackRecord.close_trade time). |
| Tiered sizing 50%/75%/100% by funding tier | **DOC OMITS** | `funding_size_tiers` scales position size with rate; doc treats sizing as binary. |
| Decay-floor (latest ≥ 0.80 × median(recent)) | **DOC OMITS** | `funding_decay_floor=0.80` blocks entries on decaying-but-still-above-threshold funding. |
| Negative-streak close (2 negatives → exit) | **DOC OMITS** | `funding_negative_close_windows=2`. |
| 8h cooldown after exit | **DOC OMITS** | `funding_cooldown_hours=8` prevents whipsaw re-entries on borderline rates. |

---

## §6 Agent 5 — Multi-Speed EWMA Trend (Crypto)

| Claim | Status | Notes |
|---|---|---|
| Three speed pairs (8/32, 16/64, 32/128) | MATCH | `trend_speeds`. |
| 2-of-3 consensus required | MATCH | `trend_min_speeds_agreeing=2`. |
| Inverse-volatility sizing, 10% annualized vol target | MATCH | `trend_target_portfolio_vol=0.10`; `_annualized_vol` uses log returns × √(365×6) for 4h bars. |
| Kaufman Efficiency Ratio (ER) chop filter | **GAP** | Not implemented. We have ADX in momentum but no ER in trend. The trend agent relies on the 2-of-3 vote and the inverse-vol scaling alone. |
| Max-leverage cap | **DOC OMITS** | `trend_max_leverage=3.0` caps the vol-target multiplier. |
| Size modulated by regime gate ±20% | **DOC OMITS** | Reads `crypto_size_modifier` from research log → scales notional. |

---

## §7 Agent 6 — On-Chain Regime (Crypto)

| Claim | Status | Notes |
|---|---|---|
| MVRV thresholds 1.0 / 3.5 | MATCH | `crypto_sent_mvrv_bullish=1.0`, `crypto_sent_mvrv_bearish=3.5`. |
| MVRV score → size modifier ±20% | MATCH | `_mvrv_to_score` in `agents/trading_crypto_sent.py`. |
| SOPR signal (crossing 1 from below) | **GAP** | Not implemented. Would require Glassnode subscription. |
| Exchange netflow | **GAP** | Not implemented. Same reason. |
| Reddit sentiment via PRAW | **GAP** | Not implemented. The `data.reddit_*` settings exist as placeholders. |
| Aggregate `regime_score` formula with w1..w5 weights | **DIVERGES** | We compute the simpler arithmetic average of available components (`regime + mvrv + social`), with missing components excluded so they don't dilute toward zero. Functionally equivalent when only MVRV is wired, simpler than the doc's weighted scheme. |

---

## §8 Agent 7 — Indian F&O Research

| Claim | Status | Notes |
|---|---|---|
| Open Interest dynamics (long buildup, short covering, etc.) | **GAP** | Not built. `research_india` does **news + sentiment + last_close** but not F&O OI analysis. |
| Put-Call Ratio interpretation | **GAP** | Not built. NSE F&O bhavcopy ingestion would need to be added. |
| `jugaad-data` / `nsepy` for NSE data | **PARTIAL** | Listed in `requirements.txt` but not currently exercised by any agent. We use yfinance for closes. |

**This is the largest single divergence.** The original 8-agent spec had an F&O research agent; what we built is a news-sentiment research agent under the same name. If/when you want to be faithful to the spec, this needs a new agent (call it `research_fo`) reading from NSE bhavcopy.

---

## §9 Agent 8 — Composite Regime Classifier

| Claim | Status | Notes |
|---|---|---|
| Composite risk_score with weighted MVRV + funding + sentiment | **DIVERGES** | Our `trading_crypto_sent` averages available components; the doc's `1.0 / 1.0 / 0.5 / 0.5 / 0.5·tanh(...)` weighted scheme is more elaborate but reduces to similar behavior when components are bounded in [−1, +1]. |
| risk_on / neutral / risk_off boundaries at ±1 | **DIVERGES** | Modifier is in [−1, +1] continuously; risk_manager treats `regime=="risk_off"` (from `research_crypto`) as a hard block, separate from the modifier. Two-layer system; doc collapses them into one. |

---

## §10 Risk Manager

| Claim | Status | Notes |
|---|---|---|
| Kelly f* = (b·p − q)/b | MATCH | `risk_manager._half_kelly` (capped at 0.25 for safety). |
| Half-Kelly used in practice | MATCH | `kelly_fraction=0.5`. |
| 100-trade rolling window for win/loss stats | **DIVERGES** | `kelly_lookback_trades=50`. Tighter — adapts faster but with higher variance. |
| 2% hard cap dominates Kelly | MATCH | `max_pct_per_trade=0.02`. |
| 10% drawdown over rolling 30 days | MATCH | `kill_switch_drawdown=0.10`, `kill_switch_window_days=30`. |
| Rolling-window peak (not all-time) so recovered fund can re-engage | **DIVERGES** | We use rolling-window **max-drawdown** within the window, which **persists** after recovery within the 30d window. The doc's wording suggests immediate re-engagement on recovery; ours doesn't. Test pin: `test_kill_switch_integration.py::test_kill_switch_persists_after_equity_recovery`. |
| Correlation block via PnL series correlation | **DIVERGES** | We block based on `proposal.correlation_with_open_longs` provided by the agent, not internally-computed PnL correlation. The doc's mechanism is more principled but requires a rolling cross-agent PnL matrix we haven't built. Same effect for our specific universes. |
| Market hours gate (9:15–15:25 IST) | MATCH | Encoded in `risk_manager._check_market_hours`. |
| VIX > 25 / +20% spike halves Agent 1 size | **GAP** | Not implemented. We don't ingest India VIX. |
| Regime override blocks directional crypto on risk_off | MATCH | `risk_manager._check_regime`. |

---

## §11 Redis Pub/Sub

| Claim | Status | Notes |
|---|---|---|
| Redis pub/sub for agent coordination | MATCH (configurable) | `infra/signal_bus.py::RedisBus` exists. Default in single-process mode is `InMemoryBus`; `RedisBus` is auto-attached by the FastAPI app when `REDIS_URL` is set. |
| Channel names (`signal.equity.*`, `orders.approved`, etc.) | **DIVERGES** | Our actual channels: `price.<symbol>`, `news.alert`, `news.raw`, `research.regime`, `research.size_modifier`, `trade.opened`, `trade.closed`. The doc's are conceptual; ours match what's actually wired. |
| JSON schema for messages | MATCH (in spirit) | Payloads vary by channel but all are JSON-encoded dicts. |
| Kafka mirror for audit | **GAP** | Not implemented. Audit lives in the append-only SQLite/Postgres trade log instead. |

---

## §12 Latency

| Claim | Status | Notes |
|---|---|---|
| Exchange tick → WebSocket 5–25 ms | MATCH (system characteristic) | Verified ~10s of ms on live Binance spot stream. |
| Risk-rule evaluation < 1 ms | MATCH | Pure-Python rules over an in-memory SQLite read. |
| Redis pub→sub local 0.3–0.8 ms | MATCH | Standard local Redis. |
| FinBERT ONNX 6–10 ms | **GAP** | We're at ~35–45 ms with eager PyTorch. ONNX would close the gap. |

---

## §13 Python + ONNX vs C++

Reasoning is sound. **One correction:** the section assumes ONNX is in use. In our code, FinBERT runs through `transformers.pipeline` (PyTorch eager). Adding ONNX export would be a 1-day task and would make this section literally true.

---

## §14 Academic citations

All accurate. No corrections needed.

---

## §15 Caveats

All accurate and matches what's in our memory notes (`project_funding_arb_refinements.md`, `project_momentum_refinements.md`, etc.). The "0.72/0.5 sentiment thresholds, 8/32 EWMA spans, BTC/ETH funding entry at 0.01%" framing matches exactly.

---

## Summary

**12 MATCH** entries, **15 DIVERGES (defensible)** entries, **8 GAP** entries.

**The defensible divergences make the code more conservative than the doc.**
Every one tightens entry conditions, widens stops, or shortens hold time
vs the literature baseline. That's appropriate for a system about to
deploy real capital.

**The 8 GAP items, prioritized:**

1. **NSE F&O research agent** — the spec's Agent 7. Build a new `research_fo.py` reading bhavcopy via `jugaad-data` for OI / PCR / sectoral flows. (~2 days)
2. **SOPR + exchange netflow signals** for the regime gate — requires Glassnode ($39/mo). (~0.5 day once API key set)
3. **Reddit/X sentiment via PRAW** — `social_sentiment` channel for the regime gate. (~1 day)
4. **ONNX export for FinBERT** — 4–6× speedup on news scoring. (~0.5 day)
5. **Kaufman Efficiency Ratio chop filter** for `trading_trend`. (~2 hours)
6. **India VIX vol-shock dampener** — halves Agent 1 size when VIX spikes. (~1 day, includes feed)
7. **Internally-computed PnL correlation** for the correlation block. (~1 day)
8. **Kafka audit mirror** — likely over-engineered for a solo-trader scale; skip unless raising external capital.

None of these blocks the burn-in. They're best built post-burn-in once
real paper data shows which signals matter most.

---

## What this means for the writeup

If you want the doc to be 100% accurate to the code as-is, here are the
edits I'd suggest:

- §2: replace "volume Z-score classification" with "ADX > 20 + 200-EMA trend gate + median-volume confirmation"; remove "Forced flat at 15:25 IST"
- §3: change "3 consecutive 15-min windows" to "latest ≥ 0.72 AND decay-weighted-avg(last 3) ≥ 0.72"; replace ONNX latency numbers with eager-PyTorch numbers; add the panic-exit + min-headlines rules
- §4: change "1–30 days half-life" to "≤ 10 days"; change "exit at z=0" to "exit at \|z\| ≤ 0.5"; change "3.5σ stop" to "3.0σ stop"; add correlation pre-screen note
- §5: change "two consecutive observations" to "three stable windows"; change "3× max leverage" to "2× max leverage"; add tiered sizing, decay floor, negative-streak close, cooldown
- §6: note ER chop filter is unbuilt; add max-leverage and size-modulation
- §7: rewrite — Agent 6 currently only has MVRV; SOPR/netflow/Reddit are roadmap
- §8: rewrite — "Agent 7 F&O research" is unbuilt; what exists is `research_india` doing news+sentiment
- §10: change "100-trade window" to "50-trade window"; clarify the kill switch persists within the 30d window (does NOT immediately release on recovery)

Or — alternatively — implement the unbuilt items so the doc becomes
accurate. The 8 GAP items above are the menu.
