# NIOS — Four-Ring Sovereign Trading System

NIOS is a four-ring, air-gapped, latency-tiered trading system. Deterministic execution lives
in Ring 0 and Ring 1; slow probabilistic cognition lives in Ring 2; a human operator holds the
controls in Ring 3. The rings exchange bounded, typed contracts and never share mutable state.

> **LLMs may reason about the market. LLMs may never sit in the microsecond execution loop.**

The binding specification is `NIOS_Core_architecture.json` (`sec_001`–`sec_018`) and the
operating constitution is `AGENTS.md`. If code, a prompt, or a convenience shortcut conflicts
with the JSON, the JSON wins.

On-call procedures — boot, kill switch, crash recovery, key rotation, monitoring and OS
tuning — live in [`RUNBOOK.md`](RUNBOOK.md).

---

## Quick start

### One click

| Platform | Command |
|---|---|
| Windows | `start_nios.bat` |
| Linux / macOS / WSL | `./start_nios.sh` |

On first use on Linux/macOS, make the script executable: `chmod +x start_nios.sh`.

Both scripts resolve a Python interpreter, verify it can import the runtime, confirm the port
is bindable, run the **23-gate release smoke test**, and only launch the unified server if every
check passes. A failed precondition or smoke test aborts the launch — a non-certified build is
never served.

```text
start_nios.bat               # smoke test, then serve on http://127.0.0.1:8000
start_nios.bat --skip-smoke  # serve only
```

### Manual

```bash
# Windows (PowerShell)
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
python scripts/run_nios.py --smoke-test
python scripts/run_nios.py --host 127.0.0.1 --port 8000
```

```bash
# Linux / WSL
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python scripts/run_nios.py --smoke-test
python scripts/run_nios.py --host 127.0.0.1 --port 8000
```

The cockpit is served at `http://127.0.0.1:8000/`; REST, SSE and the MCP bridge share the same
origin. The SPA exposes **fifteen pages** — `/dashboard`, `/terminal`, `/orders`, `/options`,
`/analyzer`, `/quant`, `/agents`, `/backtest`, `/brain`, `/forge`, `/ledger`, `/logbook`,
`/verification`, `/brokers`, `/control` — plus the aliases `/` (dashboard), `/positions`
(orders), `/chain` (options), `/xray` (brain), `/journal` (logbook), `/venues` (brokers) and
`/mcp` (control). Every page is served under the base tree, `/paper/*` and `/live/*`, and the
three auth doors are `/login` (gateway selector), `/paper/login` and `/live/login`.
Interpreter discovery order:
`NIOS_PYTHON` → `NIOS_VENV` → `.venv` → `venv` → pre-provisioned dev venv → `py -3.12` /
`python3.12`.

---

## The four rings

| Ring | Path | Responsibility | Hard boundary |
|---|---|---|---|
| **0 — Sovereign Vault** | `ring0_vault/` | Verifies SHA-256 digests, enforces whitelists and notional caps, runs the 98% split-conformal gate and the cross-silo tail-copula shield, stages two-phase TigerBeetle transfers, signs orders, reconciles every 60 s, enforces ADR-009 loss stops. | The only ring holding broker credentials or signing power. |
| **1 — Spinal Reflex** | `ring1_spinal/` | Deterministic zero-GC hot path. Dual-vendor feed normalization, executed-OFI gate, cancel-to-fill gate, 0.1 ms volatility breaker. | Holds short-TTL leases only. Cannot sign, settle or write the IBOR. |
| **2 — Bicameral Brain** | `ring2_brain/` | 490-factor zoo and valuation lab, SHA-256 grounding ledger, SEC EDGAR point-in-time guard, MiroFish contagion cascade, System 1 lease writer and the System 2 investment committee. | No broker keys, no exchange sockets, no order signing. |
| **3 — Glass Cockpit** | `ring3_cockpit/` | FastAPI REST/SSE control plane, global kill switch, the React 19 fifteen-page cockpit with separate Paper and Live shells, and a read-only MCP bridge. | Read-only for market state; may only write the explicit control endpoints. |
| **Core substrate** | `core/` | POSIX shared-memory SeqLock, NATS JetStream bus, DuckDB/Parquet bitemporal lake, orchestrator. | Tier 0/1/2 contracts only. |

### The five non-negotiable laws

1. Never trust resting orders or a single feed — require executed taker flow.
2. Never put an LLM in the microsecond loop — System 1 writes a 60 s lease, off-path.
3. Simulate contagion, hash every number — unhashed claims are rejected.
4. 99% noise abstention, 30-trade shadow graduation.
5. Compound inside the silo; every silo is self-contained with no cross-border wires.

---

## Performance: measured vs SLA

Measured on the release host on 2026-11-09 with the suite's own harness. Percentiles are over
the sample counts shown; the SLA column is the constitutional contract from `sec_010`/`sec_013`.

| Path | Measured | SLA | Sample |
|---|---|---|---|
| Ring 1 tick-to-intent | **p50 0.0125 ms · p99 0.0238 ms** @ 75,273 ticks/s | p50 ≤ 0.088 ms · p99 ≤ 0.140 ms @ 25,000 ticks/s/core | 25,000 ticks |
| SHM lease / kill-latch read | **p50 0.0006 ms · p99 0.0013 ms** (p99.9 0.0025 ms) | p50 ≤ 0.005 ms · p99 ≤ 0.008 ms | 50,000 reads |
| Ring 0 conformal + firewall + sign | **p50 0.1737 ms · p95 0.2208 ms · p99 0.2753 ms** | p50 ≤ 0.290 ms · p99 ≤ 0.420 ms | 10,000 evaluations |
| 490-factor scan (5,000 instruments → 50 candidates) | **p50 14.12 ms** | < 120 ms | 5 scans |
| MiroFish 3-tick cascade | **p50 0.0066 ms · p99 0.0169 ms** | p99 < 1.8 ms | 1,000 cascades |
| Token cost | **$3.10/day cap** (from $86.40/day, −96.4%) | ≤ $3.10/day | `TokenBudgetLedger` |

Reproduce:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  tests/test_step2_core_spine.py tests/test_step3_truth_and_quant.py \
  tests/test_step4_ring2_cognition.py tests/test_step5_ring1_spinal.py \
  tests/test_step6_ring0_vault.py -q -s
```

> These are single-host numbers. The p99 ceilings in `sec_013` assume a core-pinned real-time
> host; the suite applies an explicit 1.5× host allowance where the OS scheduler intrudes.

---

## Venue adapters: 12 / 12 ready across 4 silos

Every adapter is probed at boot. A silo whose adapters do not import cleanly cannot arm live.

| Silo | Market | Ready adapters | Vaults |
|---|---|---|---|
| `CRYPTO_BINANCE` | Crypto | `binance`, `okx`, `bybit` (3) | ReserveYield · LongTermCompounder · TacticalSwing · SpinalScalper |
| `INDIA_ZERODHA_DHAN` | India equities | `dhan`, `zerodha`, `angel`, `upstox` (4) | 4 |
| `US_IBKR` | US equities | `interactive_brokers`, `alpaca` (2) | 4 |
| `FX_MT5` | FX / futures | `vnpy_mt5`, `vnpy_ctp`, `oanda` (3) | 4 |

In-silo ratchet: **35%** long-term compounder, **15%** reserve, remainder tactical and scalp.
Capital never crosses silos. Live mode requires an explicit operator action through
`POST /api/v2/control/brokers`; paper is the default everywhere, including in tests.

---

## Formal verification & the tactical tail-hedger

Ring 3 surfaces both on the **Brain & Contagion X-Ray** (`/brain`) page and the
**Proofs, Chaos & Telemetry** (`/verification`) page, which also exposes the interactive chaos,
VOPR and OS-tuning drills described in [`RUNBOOK.md`](RUNBOOK.md) SOP 6.

**TLA+ exhaustive model checking** — four specifications explored to a fixed point by BFS:

| Spec | States | Invariants |
|---|---|---|
| `KillSwitch` | 51 | `KillSwitchIsTotal`, `NoDispatchWhileHalted`, … (5) |
| `OrderLifecycle` | 1,108 | `NoOrphanedOrders`, `BoundedExposure`, … (5) |
| `Outbox` | 16 | `AtMostOnceExecution`, `NoOverReservation`, … (6) |
| `Reconciliation` | 34,596 | `DriftFreezesSilo`, `ReconciliationSafety`, … (4) |
| **Total** | **35,771 states / 569,610 transitions** | **0 counterexamples** |

**VOPR deterministic fault injection** — 1,000 steps, seed 7, **765 faults** across 10 classes
(network drop, duplicate NATS delivery, out-of-order and duplicate acks, overdraft attempts,
power loss, split ticks, 3.5× volatility spikes, stale reconcile): **0 overdrafts, 0 conservation
violations, 0 duplicate executions**, double-entry and at-most-once both held.

**TacticalSwing options tail-hedger** — Ring 2 buys protective OTM puts when
`cascade_fragility_index >= 0.65` **and** `tail_correlation > 0.60`. Each hedge is a
Black-Scholes-priced put sealed in a SHA-256 `GroundedFactPacket`, routed to
`VaultName.TACTICAL_SWING`, and signed only by Ring 0. Live fields: status, trigger gauges,
Delta / Gamma / Vega / premium bps, contract count, hedge ratio and the packet digest.
Verified case: fragility 0.72, copula 0.68 → strike 46,500 on spot 50,000, delta −0.208,
premium 247.0 bps.

---

## Test suite

**716 tests · 32 suites · 0 failures · 0 warnings.** A warning is treated as a defect:
`pyproject.toml` sets `filterwarnings = ["error"]` and exempts only four named third-party donor
modules.

```bash
# Windows
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
python -m pytest tests -q

# Linux / WSL
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests -q
```

| # | Suite | Covers |
|---|---|---|
| 1 | `test_step2_core_spine.py` | SHM SeqLock, NATS bus, bitemporal lake |
| 2 | `test_step3_truth_and_quant.py` | 490-factor zoo, scan SLA, grounding ledger, valuation |
| 3 | `test_step4_ring2_cognition.py` | MiroFish cascade, System 1/2 switchboard |
| 4 | `test_step5_ring1_spinal.py` | Tier-0 latency + throughput, OFI/breaker gates |
| 5 | `test_step6_ring0_vault.py` | Conformal gate, firewall, signing, ADR-009 |
| 6 | `test_step7_cockpit_and_e2e.py` | Cockpit REST/SSE, kill switch, full-ring E2E |
| 7 | `test_step8_forge_mcp_and_deploy.py` | Alpha Forge, MCP air-gap, deployment |
| 8 | `test_step9_paper_autopilot.py` | Paper autopilot, rearm, runtime version contract |
| 9 | `test_phase2_step1_donor_activation.py` | Donor activation, ring isolation |
| 10 | `test_phase2_step2_silo_adapters.py` | Four silos, PAPER→LIVE toggle |
| 11 | `test_phase2_step3_feeds_and_llm_pool.py` | Dual-vendor feed, LLM pool budgets |
| 12 | `test_phase2_step4_formal_vopr_and_hedger.py` | TLA+/VOPR, tail hedger, Phase 2 certification gate (headless document served at `/`) |
| 13 | `test_phase3_step1_multiprocess_and_live_daemons.py` | 4-ring OS supervisor |
| 14 | `test_phase3_step2_live_cutover_and_cockpit.py` | Live cutover, masked credentials |
| 15 | `test_phase3_step3_testnet_supervisor_and_security.py` | Testnet bridge, supervisor, security |
| 16 | `test_phase3_step4_soak_metrics_and_chaos.py` | Soak metrics, chaos drills |
| 17 | `test_phase3_step5_os_tuning_key_rotation_and_runbook.py` | OS/socket auditor, zero-downtime Fernet + Ed25519 rotation, runbook completeness |
| 18 | `test_phase4_final_preflight_and_supervisor_forge.py` | 23-gate pre-flight certificate, supervisor forge/tail-hedge, Phase 4 release seal |
| 19 | `test_v1_1_auth_dual_workspace_and_clean_slate.py` | **V1.1** operator auth, dual workspaces, clean slate, LIVE mutation guard |
| 20 | `test_v1_2_agent_tasks_backtesting_and_daily_analysis.py` | **V1.2** 8 harvested strategies, grounded decision dashboard, 6 agent tasks, watchlist batch ranking, grounded Q&A follow-up, 5-timeframe backtester, 8-way comparison, 30-fill nursery gate |
| 21 | `test_v1_3_redesigned_dual_ui_separate_logins_and_15_pages.py` | **V1.6** separate `/paper/login` + `/live/login` portals, options chain (Greeks/PCR/Max Pain), six payoff templates, market terminal, cost-adjusted logbook, 12-venue broker hub, 15-page catalogue across all three route trees, headless document |
| 22 | `test_v1_4_clean_room_masterpiece_ui.py` | **V1.6** headless backend contract: no frontend tree, 55-route zero-JavaScript document, envelope + risk telemetry on 13 page endpoints, dual workspace boundary, clean-slate rendering |
| 23 | `test_v1_5_institutional_terminal_ui.py` | **V1.6** headless institutional contract: 50-route status document, integer micro money, exact itemized statutory floors, 15-page catalogue, dual workspace boundary, reproducible payload digest |
| 25 | `test_v1_7_deep_market_universe_and_feature_audit.py` | **V1.7** 274-instrument institutional universe (all 50 NIFTY 50, 80 crypto perps, 79 US names, 28 G10 pairs, MCX + CME), asset-class fundamental / on-chain / COT feature store, four-expiry IV term structure + GEX surface, India / US / crypto heatmaps and the G10 strength matrix, macro calendar, instrument search / filter / detail APIs, SmartOMS + walk-forward lot, tick and statutory-floor enforcement |
| 24 | `test_v1_6_backend_bug_hunt_security_and_data_completeness.py` | **V1.6** master instrument registry + lot/tick/freeze enforcement, itemized statutory cost engine, adversarial numerical/boundary fuzzing, 32-thread idempotency + double-entry conservation, auth brute-force lockout + secret non-leakage |
| 25 | `test_v1_7_deep_market_universe_and_feature_audit.py` | **V1.7** 274-instrument universe, asset-class feature store, options surface, heatmaps, macro calendar, instrument APIs |
| 26 | `test_v1_8_tradingview_bridge_streaming_and_handshake.py` | **V1.8** TradingView replay bridge, 274 `EXCHANGE:SYMBOL` tickers, eight Pine study evaluators, SSE stream hub, 12-venue handshake |
| 27 | `test_v1_8_deep_gap_closure_and_cross_engine_audit.py` | **V1.8** universal options chain, segment-aware statutory tax, circuit/margin/drawdown enforcement, crash-safe state journal, SSE disconnect fuzzing |
| 28 | `test_v1_9_mission_tranche_and_profit_split.py` | **V1.9** mission authorization, half-Kelly feasibility, tranche isolation, correlation gate, pacing/target lock, dynamic split, crypto sweeps, virtual margin quotas |
| 29 | `test_v1_9_sebi_and_regulatory_versioning.py` | **V1.9** SEBI algo-id, 10 OPS bucket + OTR, egress allowlist, TOTP renewal, freeze slicing, hedge exit ordering, effective-dated FINRA 4210 / PDT, locate fee, SSR, extended hours |
| 30 | `test_v1_9_oms_fsm_binance_and_env_truth.py` | **V1.9** staleness watermark, all-or-none baskets, cold-start recon, order FSM, clock health, exchange calendar, fencing epoch, replay checkpoints, Binance microstructure |
| 31 | `test_v1_9_ai_provenance_and_concept_drift.py` | **V1.9** provenance + attestation + freshness bound, injection guard, star aggregation, frozen signing inputs, concept drift, agent behaviour monitor, Gate 23 |
| 32 | `test_v1_9_entity_memory_and_default_feasibility.py` | **V1.9** default-on mission feasibility, `gbrain` entity post-mortem memory (timeline + compiled truths + hybrid retrieval + sealed packet), pre-trade agent memory haircut, `plannotator` inline plan annotation, MCP call session guard |


With `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `pytest` is the only plugin loaded, so the result is
independent of whatever happens to be installed in the interpreter.

---

## V1.7 - institutional multi-asset universe, deep features and catalysts

`v3.8.0-V1.7` turns the 48-instrument skeleton into a 274-instrument institutional
catalogue and gives every entry a real, SHA-256-grounded data profile:

* **274 canonical instruments, zero duplicate symbols.** 76 India (8 indices, all 50
  NIFTY 50 constituents, 14 MCX commodity futures, 4 NSE currency futures), 80 Binance
  USDT perpetuals across layer-1, DeFi, L2, AI/DePIN, RWA, gaming and meme narratives,
  79 US names (50 mega/large caps, 23 benchmark and sector ETFs, 6 CME futures) and 39
  FX/CFD contracts (all 28 G10 pairs, 5 EM crosses, 4 precious metals, 2 energy CFDs).
  Every row carries display name, venue, asset class, sub-category, sector, currency,
  lot, tick, freeze, circuit, initial/maintenance margin, settlement, UTC session,
  option strike step, `has_options` and a venue identifier.
* **Asset-class feature store.** `core/market_features_store.py` emits equity
  fundamentals (`pe_ratio`, `forward_pe`, `ev_ebitda`, `roe_pct`, `roce_pct`,
  `week_52_high/low`, `beta`, `adv_30d_units`, ownership, earnings date, relative
  strength), crypto derivatives and on-chain data (`fdv_usd`, `open_interest_usd`,
  `funding_rate_8h_pct`, `annualized_basis_pct`, `long_short_account_ratio`,
  `liquidations_24h_usd`, `onchain_active_addresses_24h`, `exchange_netflow_24h_usd`,
  `btc_dominance_pct`) and FX/commodity carry and positioning (`base_policy_rate_pct`,
  `rate_differential_bps`, `annualized_carry_pct`, `cot_net_non_commercial_lots`,
  `cot_percentile_52w`, `implied_vol_1m/3m_pct`, `risk_reversal_25d`, `active_session`,
  `pip_value_per_standard_lot`).
* **Four-expiry options surface.** `get_options_surface_profile()` returns a
  `7DTE/30DTE/60DTE/90DTE` term structure plus `iv_rank`, `iv_percentile`, 25-delta
  skew and butterfly, dealer `net_gamma_exposure_usd` (GEX), vanna, charm, PCR (OI and
  volume) and `max_pain_strike`. Non-optionable instruments still carry a modeled
  surface, flagged `modeled=true`, so the endpoint never returns a hole.
* **Cross-asset heatmaps and a macro calendar.** `ring2_brain/quant/macro_and_catalysts.py`
  aggregates the catalogue into the India NSE sector heatmap, the US GICS sector
  heatmap, the crypto narrative heatmap and the G10 currency strength matrix
  (`-100..+100` relative strength plus carry-adjusted momentum and every pair
  differential), and publishes a release calendar covering RBI, India CPI/IIP/GST,
  FOMC, US CPI/PCE/NFP/ISM, ECB, BoE, BoJ, EIA crude and the BTC quarterly expiry with
  consensus, previous, impact tier and volatility multiplier.
* **Discovery APIs.** `GET /api/v2/instruments` (filter by silo, asset class, sector,
  free-text search and option eligibility, paginated 1..500), `GET
  /api/v2/instruments/{symbol}` (contract spec + features + surface + itemized
  statutory cost + grounded decision score in one response), `GET
  /api/v2/market/heatmaps` and `GET /api/v2/market/macro-calendar`. The terminal,
  options, quant and agent pages now draw from the full universe instead of a hardcoded
  ticker list.
* **Registry enforcement is total.** `SmartOMS` rejects an unknown symbol, an off-lot
  quantity or a quantity above the exchange freeze with `422 INSTRUMENT_REJECTED`, and
  snaps an off-tick limit price onto the exact tick grid before the outbox is touched.

---

## V1.6 — headless backend, complete data and security hardening

`v3.7.0-V1.6` removes the React/Vite frontend and turns Ring 3 into a headless
REST/SSE backend:

* **No frontend ships.** `ring3_cockpit/ui/openalgo_shell/src`, `public`,
  `node_modules`, the Vite/Biome toolchain and the compiled JavaScript bundle are
  gone. Client routes answer `200 text/html` with a deterministic,
  zero-JavaScript status manifest.
* **Deterministic envelope.** Every cockpit/workspace payload carries
  `schema_version`, `generated_at_ns`, `clean_slate_active`, `workspace_mode`,
  `payload_sha256` and a per-silo `risk` block of integer micro-units
  (`gross_exposure_micros`, `margin_used_micros`, `var_99_bps`, …).
* **Master instrument registry.** 48 canonical instruments across the four silos
  with exact lot, tick, freeze, circuit, margin, settlement and session specs;
  `SmartOMS` enforces lot/tick/freeze before the outbox.
* **Itemized statutory cost engine.** Every fill, backtest and logbook row
  records STT / exchange / SEBI / stamp / GST / brokerage / slippage (and the
  crypto, US and FX equivalents) in integer micros and bps, reconciling exactly
  to `8.50 / 16.00 / 2.80 / 1.60` bps.
* **Complete quant telemetry.** 490 factors across six taxonomies with IC,
  decay half-life, z-score and weight; 47 loaders and 17 regional engines with
  full schema metadata.
* **Security hardening.** Schema-level `NaN`/`Inf` rejection, traversal-safe
  identifiers, re-entrant locks across every mutating engine, `idempotency_key`
  replay on paper orders, and a persisted exponential brute-force lockout
  (`429 TOO_MANY_ATTEMPTS`) on every auth surface.

---

## V1.1 — sovereign operator auth, dual workspaces, clean slate

`V1.1` makes the cockpit an operator-owned surface: nothing renders before an operator
credential exists, and Paper can never touch live capital.

### Two-tier sessions

| Tier | Minted by | Lifetime | Reaches |
|---|---|---|---|
| `PAPER` | `POST /api/v2/auth/setup` / `POST /api/v2/auth/login` | 12 h | `/paper/*`, read-only telemetry |
| `LIVE` | `POST /api/v2/auth/elevate-live` + numeric `live_pin` | 30 min | everything, plus the four live-mutating routes |

The session rides in `X-NIOS-Session` or the `HttpOnly; SameSite=Strict` `nios_session`
cookie. Elevation is single-use: the elevated PAPER token is revoked when the LIVE token is
issued. SPA entry points are `/setup`, `/login` and `/live-unlock`.

| Endpoint | Effect |
|---|---|
| `GET /api/v2/auth/status` | `{initialized, authenticated, username, tier, clean_slate_active}` |
| `POST /api/v2/auth/setup` | First-run operator registration → PAPER session |
| `POST /api/v2/auth/login` | PAPER session |
| `POST /api/v2/auth/elevate-live` | LIVE session (second factor) |
| `POST /api/v2/auth/logout` | Revoke the presented session |
| `POST /api/v2/auth/reset` | Delete the credential **and** zero the workspace — requires `{password, live_pin}` once initialized |

### Two physical workspaces

`/paper/*` (10 routes) and `/live/*` (8 routes) are separate React trees with separate state
contracts. `GET /api/v2/workspace/paper/state` is Paper-scoped; `GET /api/v2/workspace/live/state`
answers `401 AUTH_REQUIRED` with no session and `403 LIVE_ELEVATION_REQUIRED` with a PAPER
session. Legacy routes (`/dashboard`, `/orders`, `/ledger`, `/control`, `/`) still resolve.

### Clean-Slate first boot

A fresh install comes up with **no demo money and no running tape**: every silo × vault
balance `0`, `silo_nav_micros == 0`, `orders == []`, `reservations == []`, `shadow_fills == 0`,
`token_spend_usd == 0.0`, `autopilot.enabled == false`, `clean_slate_active == true`.

```bash
# Operator funds their own paper capital (double entry, canonical vault split)
curl.exe -X POST http://127.0.0.1:8000/api/v2/control/ledger/fund-paper \
  -H "X-NIOS-Session: $PAPER_TOKEN" -H "Content-Type: application/json" \
  -d '{"silo":"US_IBKR","amount_units":25000.0,"vault":"ALL_CANONICAL"}'

curl.exe -X POST http://127.0.0.1:8000/api/v2/control/workspace/reset -H "X-NIOS-Session: $PAPER_TOKEN"
curl.exe -X POST http://127.0.0.1:8000/api/v2/control/workspace/autopilot \
  -H "X-NIOS-Session: $PAPER_TOKEN" -H "Content-Type: application/json" -d '{"enabled":true}'
```

### LIVE mutation guard

Once `operator_auth.initialized()` is `true`, these routes refuse PAPER sessions with
`403 LIVE_ELEVATION_REQUIRED` (or `401 AUTH_REQUIRED` when unauthenticated):

| Route | Guarded when |
|---|---|
| `POST /api/v2/control/brokers` | `mode == "LIVE"` (PAPER de-escalation stays open) |
| `POST /api/v2/control/brokers/credentials` | always |
| `POST /api/v2/control/brokers/testnet-order` | always |
| `POST /api/v2/control/vault/rotate-keys` | always |

On a first-run install with no credential on disk the guard is inert, so pre-V1.1 behaviour is
preserved exactly.

---

## V1.2 — agent task hub, grounded Q&A, multi-timeframe backtester

`V1.2` harvests `ZhuLinsen/daily_stock_analysis` into Ring 2 and exposes it through two new
cockpit pages. Everything stays research-only: no agent signs an order, reads a broker key, or
writes the IBOR.

### `/agents` — specialist agents, watchlist batch, grounded Q&A

| Endpoint | Effect |
|---|---|
| `POST /api/v2/control/agents/assign` | Dispatch one of 6 specialist agents (decision dashboard, technical, valuation, contagion, forge, backtest audit) |
| `POST /api/v2/control/agents/batch-watchlist` | One decision-dashboard batch across a symbol watchlist; ranks by `signal_score` (`0..100`) and `operation_advice` (`STRONG_BUY → AVOID`), then seals the whole ranking into one SHA-256 `GroundedFactPacket` |
| `POST /api/v2/control/agents/followup` | Multi-turn grounded strategy Q&A against a completed `task_id`; cites only verified indicators, entry/target/stop levels and risk factors, appends to `conversation_history`, and re-verifies with `NumericalGroundingVerifier` |
| `GET /api/v2/control/agents/tasks/{task_id}` | Full report plus the Q&A thread |
| `POST /api/v2/control/agents/cancel` | Cancel one task or clear the queue |

A follow-up on a task that is missing (`404`), not completed, or whose answer cannot be grounded
is refused — the answer is never returned unverified.

### `/backtest` — five timeframes and an eight-strategy comparison

`POST /api/v2/control/backtest/run` accepts `timeframe: "1m" | "5m" | "15m" | "1h" | "1d"`
(default `1d`). Shorter bars scale both per-bar volatility (`1/sqrt(bars_per_day)`) and the
annualisation factor, so a minute bar cannot inherit a daily-sized shock. Bars per year:
`1m 98,280 · 5m 19,656 · 15m 6,552 · 1h 1,638 · 1d 252`.

`POST /api/v2/control/backtest/compare-strategies` runs all **8 harvested strategies** on the
exact same bar series and returns a ranked leaderboard (`rank`, `strategy_id`,
`total_return_pct`, `sharpe_ratio`, `deflated_sharpe_ratio`, `passes_dsr_gate`, `net_edge_bps`,
`win_rate_pct`, `grounding_sha256`). Every trade is charged the silo's statutory round-trip
floor before the gate is evaluated: `INDIA 8.50 bps` (incl. 5.50 bps STT), `CRYPTO 16.00 bps`,
`US 2.80 bps`, `FX 1.60 bps`.

---

## V1.3 — separate login portals, options lab, terminal, logbook and broker hub

`V1.3` splits the cockpit's single login into **two dedicated portals** and grows the
workstation from eleven to fifteen pages.

**Two doors, two tiers.** `POST /api/v2/auth/login/paper` (`{username, password}`) mints a
12-hour `PAPER` session; `POST /api/v2/auth/login/live` (`{username, password, live_pin}`)
verifies both the `scrypt` password digest and the `pbkdf2_hmac` PIN digest in constant time
inside Ring 0 and mints a 30-minute `LIVE` session in one atomic request. A missing or wrong
PIN is always `403 LIVE_PIN_REJECTED`; a wrong password is `401 INVALID_CREDENTIALS`. The
legacy `/api/v2/auth/login` + `/api/v2/auth/elevate-live` pair still works. `GET
/api/v2/auth/status` advertises `login_portals` and `active_workspace`.

**Four new engines.** `/options` prices an 11-strike (configurable) Call/Put chain with
Black-Scholes Greeks, PCR, Max Pain and ATM IV, and prices six multi-leg templates
(`BULL_CALL_SPREAD`, `BEAR_PUT_SPREAD`, `SHORT_STRADDLE`, `LONG_STRANGLE`, `IRON_CONDOR`,
`PROTECTIVE_PUT`) into a 25-point expiry curve. `/terminal` renders a deterministic 60-bar
frame with `MA5/MA10/MA20` + VWAP, a 10-level L2 ladder with an imbalance ratio and a Lee-Ready
classified tape. `/logbook` records cost-adjusted round trips (gross → statutory bps → net →
R-multiple). `/brokers` renders the 12-venue matrix with masked credentials, an Ed25519
dry-run signature probe and dual-vendor feed health. Every response is sealed with a verified
SHA-256 `GroundedFactPacket`.

**Two shells that cannot be confused.** `/paper/*` renders the lavender/cyan Paper Sandbox
ribbon (simulated NAV, autopilot play/pause, deposit paper capital, reset to zero, switch to
live vault). `/live/*` renders the crimson/amber hazard-striped Live Production ribbon
(`🔒 LIVE PRODUCTION — REAL CAPITAL`, 23-gate status, live TTL countdown, `<1ms` kill switch,
lock live vault). No page under `/live/*` mounts a paper deposit or shadow-burst control.

---

## The fifteen-page cockpit workstation

The Ring 3 cockpit is a React 19 SPA with fifteen pages. `GET /api/v2/cockpit/pages` is the
authoritative catalogue; the SPA catch-all serves any route (and alias) on direct navigation or
refresh, while `/api/*` and `/metrics` still return a real `404`.

The sidebar is collapsible and grouped into five sections: **Core Execution**, **Quant &
Options**, **Bicameral Brain & Agents**, **Ledger & Verification**, **Sovereign Control**. The
`/paper/*` tree renders the lavender/cyan Paper Sandbox shell; the `/live/*` tree renders the
crimson/amber hazard-striped Live Production shell, which never mounts a paper deposit or
shadow-burst control.

| # | Page | Route | Aliases | Ring | What it shows |
|---|---|---|---|---|---|
| 1 | Dashboard | `/dashboard` | `/` | Ring 3 | 4-silo IBOR vault cards, SSE proof stream, 60 s leases |
| 2 | Market Terminal | `/terminal` | — | Ring 1 | Multi-silo watchlist, 60 bars + MA5/10/20 + VWAP, 10-level L2 ladder, Lee-Ready tape |
| 3 | Orders & Positions | `/orders` | `/positions` | Ring 0 | SmartOMS blotter, Maker-Peg/TWAP slicers, atomic outbox |
| 4 | Options & Strategy Lab | `/options` | `/chain` | Ring 2 | Call/Put chain with Greeks, PCR, Max Pain, six multi-leg payoff templates |
| 5 | Shadow Analyzer | `/analyzer` | — | Ring 1 | 30-fill L2 shadow nursery, adverse-selection telemetry |
| 6 | Quant & Valuation Lab | `/quant` | — | Ring 2 | 490-factor scanner, DCF/Black-Scholes, loader readiness |
| 7 | Agent Task Hub | `/agents` | — | Ring 2 | 6 specialist agents, watchlist batch ranking, grounded Q&A, token ledger |
| 8 | Walk-Forward Backtester | `/backtest` | — | Ring 2 | 5 timeframes, 8-strategy tax-net comparison, Purged K-Fold, DSR gate |
| 9 | Brain & Contagion X-Ray | `/brain` | `/xray` | Ring 2 | Ring isolation, MiroFish cascade, committee, SHA-256 proofs |
| 10 | Alpha Forge & Plugins | `/forge` | — | Ring 2 | Nocturnal Forge leaderboard, plugins, SKILL.md catalog |
| 11 | IBOR Ledger & Tax | `/ledger` | — | Ring 0 | Double-entry journal, 35/15 ratchet, TigerBeetle + PG16 |
| 12 | Trade Logbook | `/logbook` | `/journal` | Ring 0 | Cost-adjusted journal, net P&L, R-multiple, win/loss attribution |
| 13 | Proofs, Chaos & Telemetry | `/verification` | — | Ring 0 | TLA+ BFS proofs, VOPR campaign, OS tuning, metrics |
| 14 | Broker Hub & Feeds | `/brokers` | `/venues` | Ring 0 | 12-venue matrix, masked credentials, Ed25519 dry-run, feed health |
| 15 | Control Room & MCP | `/control` | `/mcp` | Ring 3 | 23-gate pre-flight, credential vault, LLM switchboard, MCP |

---

## Operator endpoints

The cockpit is read-only for market state. Market-data reads are unauthenticated and
side-effect-free; the only writes are the explicit control surfaces below (`sec_016`).

### Read-only page telemetry

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v2/cockpit/pages` | 15-page catalogue, alias map, platform counts |
| `GET` | `/api/v2/cockpit/terminal` | Watchlist, 60-bar OHLCV + MA/VWAP, L2 ladder, Lee-Ready tape |
| `GET` | `/api/v2/cockpit/orders` | SmartOMS blotter, slicers, outbox pending |
| `GET` | `/api/v2/cockpit/options` | Underlyings, six payoff templates, grounded chain |
| `GET` | `/api/v2/cockpit/analyzer` | Shadow nursery and adverse-selection telemetry |
| `GET` | `/api/v2/cockpit/quant` | 490-factor scan, valuation, loader readiness |
| `GET` | `/api/v2/cockpit/forge` | Forge leaderboard, plugins, SKILL.md catalog |
| `GET` | `/api/v2/cockpit/ledger` | Double-entry journal, ratchet, TigerBeetle + PG16 |
| `GET` | `/api/v2/cockpit/logbook` | Cost-adjusted trade journal and aggregates |
| `GET` | `/api/v2/cockpit/agents` | Agent catalog, task queue, token ledger |
| `GET` | `/api/v2/cockpit/backtest` | Strategy catalog, timeframes, cost floors, saved runs |
| `GET` | `/api/v2/cockpit/brokers` | 12-venue matrix, credential status, Ed25519 dry-run |
| `GET` | `/api/v2/cockpit/mcp` | Read-only MCP catalogue + air-gap audit |
| `GET` | `/api/v2/cockpit/skills/{skill_name}` | One SKILL.md document as structured Markdown |

### Core read and control surfaces

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Compiled React 19 cockpit (`dist/`) |
| `GET` | `/api/v2/cockpit/health` | Ring status, `version`, configured venues |
| `GET` | `/api/v2/cockpit/state` | Full hydration snapshot (silos, leases, contagion, LLM, proofs) |
| `GET` | `/api/v2/cockpit/formal-proofs?refresh=` | TLA+/VOPR/backend/hedger telemetry; `refresh=true` re-runs BFS + VOPR |
| `GET` | `/api/v2/cockpit/stream` | SSE cockpit frames at 60 FPS |
| `POST` | `/api/v2/control/brokers` | Paper/Live hot-swap, cost floors, allocation, loss stop |
| `POST` | `/api/v2/control/llms` | LLM provider/model/budget; API keys are write-only |
| `POST` | `/api/v2/control/killswitch` | Global halt; disarms leases and flattens within 1.0 ms |
| `GET`/`POST` | `/api/v2/control/extensions` | Skills, plugins, MCP tools, Alpha Forge audit |
| `GET`/`POST` | `/api/v2/control/feeds` | Market-data mode hot-swap (`PAPER_AUTOPILOT` / `LIVE_PUBLIC_DUAL_VENDOR`) |
| `GET`/`POST` | `/api/v2/control/brokers/credentials` | Masked inventory / write-only venue credential handshake |
| `GET`/`POST` | `/api/v2/control/supervisor` | 4-ring OS process start / stop / restart / status |
| `GET`/`POST` | `/api/v2/control/brokers/testnet` · `/testnet-order` | Ring 0 sandbox bridge state / dispatch |
| `POST` | `/api/v2/control/llms/probe` | Local LLM probe under the 250 ms hard cap |
| `POST` | `/api/v2/control/rearm` | Explicit human reset from `HALTED` back to `ARMED` |
| `POST` | `/api/v2/control/vault/rotate-keys` | Zero-downtime Fernet master-key + Ed25519 rotation |
| `GET` | `/api/v2/control/vault/status` | Key version, public fingerprint, archived keys, OS tuning |
| `GET` | `/api/v2/metrics` · `/metrics` | Prometheus / OpenMetrics exposition |
| `GET` | `/api/v2/metrics/json` | JSON telemetry for the cockpit chip |
| `GET` | `/api/v2/mcp/tools` | Read-only MCP tool catalogue + air-gap audit |
| `POST` | `/api/v2/mcp/call` · `/api/v2/mcp` · `/api/v2/mcp/rpc` | MCP invocation (JSON-RPC + SSE) |

### Interactive workstation controls (12)

Added in Phase 4 Steps 5–6. Each is explicit, audited and reversible; none places a live order,
opens a venue socket or unlocks a silo.

| Method | Path | Effect |
|---|---|---|
| `POST` | `/api/v2/control/orders/paper` | Grounded paper order → outbox reservation → `SmartOMS` → 35/15/50 ratchet. No venue field exists. |
| `POST` | `/api/v2/control/orders/cancel` | Cancel one order id or `ALL`; voids the matching reservation. |
| `POST` | `/api/v2/control/analyzer/shadow-burst` | Drive `rounds` autopilot ticks toward the 30-fill shadow threshold. |
| `POST` | `/api/v2/control/quant/price-option` | Black-Scholes price + Greeks sealed in a SHA-256 `GroundedFactPacket`. |
| `POST` | `/api/v2/control/quant/scan` | 490-factor scan → top `k` candidates. |
| `POST` | `/api/v2/control/ledger/reconcile` | `LiveReconciler.reconcile_all` + double-entry conservation proof. |
| `POST` | `/api/v2/control/verification/chaos-drill` | Deterministic SIGKILL mid-outbox drill on an isolated SHM segment. |
| `POST` | `/api/v2/control/verification/vopr` | Seeded VOPR campaign; same seed ⇒ same fault histogram. |
| `POST` | `/api/v2/control/verification/os-tune` | Apply + audit OS low latency; privileged rows stay `RECOMMENDED`. |
| `POST` | `/api/v2/control/brain/mirofish-shock` | Five-archetype, three-tick contagion cascade with reproducible digest. |
| `POST` | `/api/v2/control/brain/committee-debate` | One grounded System 2 debate turn; `symbol=AUTO` debates the top candidate. |
| `POST` | `/api/v2/control/forge/evaluate-challenger` | Nursery gate: Deflated Sharpe `>= 1.50` **and** `shadow_fills >= 30`, read from the forge. |

### V1.2 agent and backtest controls (5)

| Method | Path | Effect |
|---|---|---|
| `POST` | `/api/v2/control/agents/assign` | One grounded specialist-agent task |
| `POST` | `/api/v2/control/agents/batch-watchlist` | Ranked decision-dashboard batch sealed into one SHA-256 packet |
| `POST` | `/api/v2/control/agents/followup` | Grounded multi-turn Q&A against a completed task |
| `POST` | `/api/v2/control/backtest/run` | Tax-net walk-forward run at `1m`/`5m`/`15m`/`1h`/`1d` |
| `POST` | `/api/v2/control/backtest/compare-strategies` | All 8 strategies on one tape, ranked, tax-net |

A rejected challenger, a drill that reports a violation, or a `NO_TRADE` committee verdict is
data, not an error. Never widen a threshold or bypass Ring 0 to make a red row green.

---

## Repository map

```text
core/                      shared substrate + orchestrator
ring0_vault/               kernel/ ibor/ silos/
ring1_spinal/              zero-GC hot path + feed adapters
ring2_brain/               quant/ truth/ mirofish/ cognition/
ring3_cockpit/             gateway/ ui/openalgo_shell/
specs/                     TLA+ models: KillSwitch, OrderLifecycle, Outbox, Reconciliation
tests/                     716 tests across 32 suites
scripts/run_nios.py        smoke test + unified server
scripts/nios_port_check.py free-port resolver (--scan 8000 8010)
scripts/preflight_golive.py 23-gate go-live readiness certificate (--strict / --fast)
scripts/tune_os_performance.py  OS/socket low-latency audit (--check/--apply/--json)
scripts/soak_test_harness.py    accelerated soak + memory-leak audit
RUNBOOK.md                 production SOPs 1-6
start_nios.bat / .sh       1-click launch
```

`_donor_staging/` is reference material only; runtime code must never import from it.

---

## Release seal - `v3.6.0-V1.5`

| Gate | Command | Required result |
|---|---|---|
| Unit / integration | `pytest tests -q` | **462 passed**, 0 failed, 0 warnings |
| Smoke suite | `python scripts/run_nios.py --smoke-test` | **23/23 rows**, `EXIT=0` |
| Go-live certificate | `python scripts/preflight_golive.py --fast` | **23/23 PASS**, `EXIT=0` |
| Live `V1.5` burn-in | boot on `:8001` with the flat terminal bundle, walk `/setup`, `/paper/login`, `/live/login` and all 45 page routes, then shut down | **50/50 routes at 200**, clean slate on boot, port `8001` free afterwards, empty `git status --porcelain` |

The seal asserts nothing new about live capital: every silo still boots `PAPER`, the 23-gate
certificate still governs a LIVE cutover, and `main` (`9a8ac5f` / `v3.0.0-PROD`), `V1`
(`ff67b5c` / `v3.1.0-V1`), `V1.1` (`99ea43a` / `v3.2.0-V1.1`), `V1.2` (`874d817` / `v3.3.0-V1.2`),
`V1.3` (`05c8575` / `v3.4.0-V1.3`) and `V1.4` (`46195f3` / `v3.5.0-V1.4`) are untouched; the
release ships on `V1.5`.

---

## Phase status

| Phase | Scope | Status |
|---|---|---|
| 1 | Sovereign Kernel & Truth Layer | Complete |
| 2 | Bicameral Brain & Conformal Gate | Complete |
| 3 | Multi-Silo OMS & Copula Shield | Complete |
| 4 | Nocturnal Alpha Forge & Kernel Bypass | Deferred (authorized incrementally) |

MCP, dynamic plugins and skills are implemented strictly as the read-only, shadow-gated
surfaces described above; nothing in the air-gap contract is relaxed by their presence.

---

## Safety notes

- Paper mode is the default in every silo and every test. Live mode requires an explicit,
  audited operator action.
- Cross-border wires are forbidden; each silo compounds internally under its own ratchet.
- Every numeric claim in a proposal, card or thesis carries a SHA-256 commitment and a
  resolvable source fact.
- The kill switch disarms all leases, cancels resting orders, voids reservations and flattens
  positions — budget `< 1.0 ms`.
