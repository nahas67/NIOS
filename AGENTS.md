# AGENTS.md - NIOS Constitutional Operating Instructions

**Document ID:** NIOS-ARC-2026-V3
**Version:** 3.0.0-PROD
**Status:** Phase 1 - Sovereign Kernel & Truth Layer (in progress)
**Source of truth:** `NIOS_Core_architecture.json` (18 sections, `sec_001`..`sec_018`)

This file is the binding operating constitution for every human or AI agent working in this
repository. If code, a prompt, a skill, or a convenience shortcut conflicts with
`NIOS_Core_architecture.json`, the JSON wins. Do not invent folders, features, libraries,
endpoints, asset classes, or data sources outside that specification.

---

## 1. Prime Directive

NIOS is a four-ring, air-gapped, latency-tiered trading system. The central rule is:

> **LLMs may reason about the market. LLMs may never sit in the microsecond execution loop.**

The system separates deterministic, zero-GC execution (Ring 1 / Ring 0) from slow,
probabilistic cognition (Ring 2) and human control (Ring 3). The rings communicate through
bounded, typed contracts only. There is no shared mutable state between Ring 2 and broker
credentials.

---

## 2. The Four-Ring Air-Gapped Architecture (`sec_004`)

### 2.1 `ring0_vault/` - Sovereign Vault

The only ring allowed to hold broker credentials, sign orders, or mutate the ledger.

| Path | Contract |
|---|---|
| `ring0_vault/kernel/` | Nautilus `RiskEngine` implementing ADR-009 (`HALTED` and `REDUCING` fast paths), and the Smart OMS with Maker-Peg and TWAP execution. |
| `ring0_vault/ibor/` | TigerBeetle two-phase atomic outbox and PostgreSQL 16 double-entry Investment Book of Records. Money is 128-bit integers. |
| `ring0_vault/silos/` | Encrypted broker adapters, the 98% split-conformal execution gate, and the cross-silo tail-copula shield. |

Ring 0 responsibilities:

- Verify the SHA-256 digest on every Ring 2 `UnsignedProposal`.
- Enforce instrument whitelists and notional caps (`<=5%` of the scalp vault, `<=12%` of the
  compounder vault).
- Run the 98% split-conformal gate and the cross-silo tail-copula shield.
- Stage the transfer in TigerBeetle as a two-phase pending transfer.
- Sign outbound orders through the Fernet/Ed25519 OS keystore.
- Freeze a silo on reconciliation drift `>0.01%`; reconcile every 60 seconds.
- Enforce ADR-009 daily-loss stops by locking the silo and switching it to reduce-only.

### 2.2 `ring1_spinal/` - Spinal Reflex

Deterministic, zero-GC hot path. No Python object allocation, no LLM call, no blocking I/O.

- Target: `<0.14 ms p99` tick-to-intent at `25,000 ticks/sec/core` (`p50 0.088 ms`).
- Runtime: Rust/Cython NautilusTrader ring buffers plus Python 3.12 `uvloop`.
- Dual-vendor self-healing market-data feed.
- Executed taker OFI gate: `TakerBuyRatio_5s >= 0.65`.
- Cancel-to-fill ratio gate: `< 3.0`.
- Volatility spike breaker: `0.1 ms`, trips at `>= 3.5x` the 5-second baseline.
- Ring 1 holds only short-TTL execution leases. It cannot sign, settle, or write the IBOR.
- Feed quarantine: drop a vendor on `lag > 5,000 ms`, `vendor divergence > 1.50%`, or a
  split guard `>= 45%`.

### 2.3 `ring2_brain/` - Bicameral Brain

Air-gapped from broker keys and exchange sockets. Ring 2 never receives a credentialed
connection. It emits only fixed-schema, unsigned proposals with a SHA-256 hash of every
numeric claim.

| Path | Contract |
|---|---|
| `ring2_brain/quant/` | Vibe-Trading 480+ Factor Zoo (Alpha101, GTJA191, Qlib158, Fundamental PIT), DCF/Comps/3-Statement valuation, options, market impact, Deflated Sharpe, 33 loaders, 12 regional engines; Trading-R1 analytical compute tools. |
| `ring2_brain/truth/` | SHA-256 Grounding Ledger, Trading-R1 arithmetic verifier, SEC EDGAR PIT guard (`statementKnowledgeTime`), multi-vendor router, delayed settlement. |
| `ring2_brain/mirofish/` | Five-archetype, three-tick contagion cascade. Revokes Ring 1 long leases when `cascadeFragilityIndex >= 0.75`. |
| `ring2_brain/cognition/` | System 1 (`<80 ms` Ollama lease writer) and System 2 (TradingAgents staged investment committee). |

Ring 2 banned operations:

- No broker API keys, no exchange WebSocket, no order signing.
- No unhashed numerical claim may reach an execution proposal. The grounding verifier aborts
  with `UNGROUNDED-NUMERICAL-CLAIM`.
- No lookahead: every fact must satisfy `knowledgeTime <= asOfTime`.
- No direct write to TigerBeetle, PostgreSQL, or a broker.

### 2.4 `ring3_cockpit/` - Glass Cockpit

| Path | Contract |
|---|---|
| `ring3_cockpit/gateway/` | FastAPI REST/SSE control plane and the global kill switch (`<1.0 ms`). |
| `ring3_cockpit/ui/` | React 19 three-tab cockpit. |

The three cockpit tabs are fixed:

1. Plain-English SSE cards with proof telemetry.
2. Contagion and lease X-ray.
3. Hot-swap control room (broker mode, LLM keys, kill switch).

The cockpit is read-only for market state. It may write only the explicit control endpoints
defined in `sec_016`:

- `GET /api/v2/cockpit/stream`
- `POST /api/v2/control/brokers`
- `POST /api/v2/control/llms`
- `POST /api/v2/control/killswitch`

### 2.5 `core/` - Shared Substrate

| Component | Contract |
|---|---|
| POSIX Shared Memory SeqLock | `<0.008 ms` p99 lease/kill-latch read; atomic, lock-free, single-writer. |
| NATS JetStream | Durable bus, `<1.5 ms`, at-least-once plus idempotency keys. |
| DuckDB + Parquet | Bitemporal feature store, partitioned by `(silo, sym, asOfDate)`, indexed on `knowledgeTs <= asOfTs`. |

---

## 3. The Five Non-Negotiable Constitutional Laws (`sec_018`)

### Law 1 - Never trust resting orders or a single feed

Every market view is dual-vendor. Normalize both feeds independently. A resting order is a
hypothesis, not liquidity. Require executed taker flow, `TakerBuyRatio_5s >= 0.65`, and
`CancelToFill < 3.0` before treating a quote as real.

### Law 2 - Never put an LLM in the microsecond loop

System 1 may write only a 60-second TTL `System1Lease` off the hot path. Tier 0 remains
deterministic: `<0.14 ms p99`, `0.10 ms` breaker, zero LLM tokens on quiet tape. If System 1
dies, the dead-man lease expires and Ring 1 stops initiating.

### Law 3 - Simulate contagion, hash every number

Run the five-archetype MiroFish cascade for three ticks before any deep thesis. Every figure
in a thesis or proposal carries a SHA-256 commitment. Reject unhashed numbers. Do not treat a
single-asset narrative as a portfolio truth.

### Law 4 - 99% noise abstention and 30-trade shadow graduation

The default answer is no trade. The 98% split-conformal gate requires
`Net_LB = E[r] - (Spread + 2*Fee + Tax + Q_0.98(Residuals)) > 0 bps`. A strategy must survive
at least 30 L2 shadow fills before live graduation. The expected abstention rate is ~99.1%.

### Law 5 - Compound inside the silo, evolve every midnight

Each silo is self-contained: 35% long-term compounder plus 15% reserve, with zero
cross-border wires. Capital does not cross silos to chase a trade. Strategy evolution runs at
`00:00 UTC`; demote a champion below `+0.50 bps`, and promote a challenger only with Deflated
Sharpe `>= 1.50` across 30+ shadow fills.

---

## 4. Four-Tier Storage Contracts (`sec_009`)

### Tier 0 - POSIX Shared Memory (`<10 us`)

- `shm://nios-active-leases` - 60-second TTL System 1 leases.
- `shm://nios-global-kill-latch` - atomic global halt latch.
- 5-second OFI and cancel-to-fill ring buffers.
- L2 shadow book.
- Atomic SeqLock. Single writer per slot. No heap allocation on the read path.

### Tier 1 - NATS JetStream (`<1.5 ms`)

- `stream.ring1.scalp-proposals`
- `stream.ring2.deep-theses`
- `stream.ring0.execution-events`
- `stream.ring3.cockpit-cards`

Delivery is at-least-once. Every producer supplies an idempotency key. Consumers must be
idempotent. No fire-and-forget critical messages.

### Tier 2 - DuckDB + Parquet Bitemporal Feature Lake

- Partition key: `(silo, sym, asOfDate)`.
- Bitemporal index: `knowledgeTs <= asOfTs`.
- Stores 480+ alpha factors, DCF/Comps outputs, and the rolling 200-trade conformal residual
  window.
- Append-only and immutable. Corrections are new rows, never in-place updates.

### Tier 3 - TigerBeetle + PostgreSQL 16

- TigerBeetle: two-phase atomic outbox, four vaults per silo, 128-bit integer amounts.
- PostgreSQL 16: double-entry IBOR, multi-FX tax lots (India STT/TDS), SHA-256 grounding hash
  store.
- Four vaults per silo: `ReserveYield`, `LongTermCompounder`, `TacticalSwing`,
  `SpinalScalper`.
- Two-phase outbox plus 60-second reconciliation. Drift `>0.01%` freezes the silo.
- Delayed settlement grades predictions and updates the 200-trade conformal window and the
  Darwinian trust multiplier (`0.8x` to `1.5x`).
- Durability validation: TigerBeetle VOPR plus serializable ACID tests.

---

## 5. Quantitative SLAs (`sec_010`, `sec_013`)

| Capability | Target |
|---|---|
| Ring 1 spinal scalper | `0.14 ms p99` tick-to-intent at `25,000 ticks/sec/core` (`0.088 ms p50`) |
| System 1 lease writer | `48 ms p50` / `78 ms p99`; `64 ms p95`; hard cap `250 ms`; refreshed every 30 s or on a headline shock |
| 480+ factor and valuation scan | 5,000 instruments in `<120 ms` (`85 ms p50` / `118 ms p99`) |
| MiroFish contagion | `1.8 ms` for a three-tick cascade |
| TradingAgents committee | `8-15 s` (`9.40 s p50` / `14.20 s p99`) |
| Conformal gate + shadow twin | 98% coverage; `0.42 ms p99` gate; 30 L2 shadow fills |
| Smart OMS and in-silo ratchet | `<2.0 ms` outbox; 3-second maker peg, max 2 re-pegs; TWAP/Iceberg `<=10%` of top book; sweep 50% of scalp profit |
| Alpha Forge | Nightly at `00:00 UTC`; Deflated Sharpe `>=1.50`; retire champion below `+0.50 bps` |
| SHM lease/kill read | `0.005 ms p50` / `0.008 ms p99` |
| Ring 0 conformal + firewall + sign | `0.290 ms p50` / `0.420 ms p99` |
| Global kill switch | `<1.0 ms` |
| Token cost | `$86.40/day` -> `$3.10/day` (`-96.4%`), `2,140x` faster ticks |

Funnel discipline: `5,000` instruments -> `50` candidates -> `3-8` trades. Tax-net cost floors
are preloaded per silo (for example India STT `5.5 bps`) before a trade can pass.

---

## 6. Security, Failures, and Circuit Breakers

### 6.1 Air-gap contract

- Ring 2 has no broker keys and no exchange sockets.
- Ring 2 emits `Fixed-Schema UnsignedProposal` plus SHA-256 hash only.
- Ring 0 verifies digest, whitelist, notional cap, conformal gate, and copula shield before
  staging a TigerBeetle pending transfer.
- Ring 1 may send unsigned scalp proposals to Ring 0. It may not sign.
- Ring 3 may control modes and kill switches. It may not place orders.
- Cross-border wires are forbidden. Each silo compounds internally.

### 6.2 Three-tier breakers

| Tier | Trigger | Action |
|---|---|---|
| T1 microstructure | 5-second volatility `>=3.5x`, 2 adverse fills, or spread cap breach | `0.1 ms` local halt/reduce |
| T2 contagion/copula | `cascadeFragilityIndex >=0.75` or cross-silo heat `>25% NAV` | Revoke long leases, shrink size |
| T3 vault/ADR-009 | Daily loss stop | Lock the silo, reduce-only, require human reset |

### 6.3 Fatal flaws this architecture exists to prevent (`sec_002`)

1. LLM tick-latency in the execution path.
2. Spoofed L2 liquidity from a single feed.
3. Two-body debate blindness (bull/bear only).
4. Conformal cold-start overconfidence.
5. Cross-broker wire and tax bleed.
6. Prompt-injection exposure of broker keys.
7. Lookahead and hallucinated arithmetic.

Every design change must state which flaw it protects against and which law it preserves.

---

## 7. Deferred Scope - Do Not Build Yet

The following are explicitly deferred until the **Main Core** (Phases 1-3: Sovereign Kernel &
Truth Layer, Bicameral Brain & Conformal Gate, Multi-Silo OMS & Copula Shield) is complete and
verified:

- **MCP** - no MCP server, no MCP client, no read-only Ring 3 MCP bridge.
- **Dynamic plugins** - no runtime plugin loader, no third-party strategy packages.
- **Skills** - no agent skill runtime, no skill marketplace, no self-modifying prompts.
- **Phase 4 Nocturnal Alpha Forge** - no `00:00 UTC` walk-forward loop, no Purged K-Fold
  promotion engine, no Rust/DPDK sub-20us kernel bypass, no options tail hedge.

Do not scaffold placeholder implementations for deferred items. A future step will authorize
them explicitly.

---

## 8. Repository Map and Donor Provenance

The repository skeleton is fixed:

```text
core/
ring0_vault/
  kernel/
  ibor/
  silos/
ring1_spinal/
ring2_brain/
  quant/
  truth/
  mirofish/
  cognition/
ring3_cockpit/
  gateway/
  ui/
specs/
tests/
_donor_staging/        # reference only; never imported by runtime code
```

### 8.1 Harvested donor code (Step 1)

| Target | Donor source | Notes |
|---|---|---|
| `ring2_brain/quant/vibe_trading/factors/` | `HKUDS/Vibe-Trading` `agent/src/factors` | Alpha101, GTJA191, Qlib158, Fundamental PIT (483 factor files). |
| `ring2_brain/quant/vibe_trading/quantlib/` | `.../agent/src/quantlib` | DCF, Comps, 3-Statement, options, market impact, Deflated Sharpe, copula. |
| `ring2_brain/quant/vibe_trading/loaders/` | `.../agent/backtest/loaders` | 33+ dual-vendor loaders and registry. |
| `ring2_brain/quant/vibe_trading/engines/` | `.../agent/backtest/engines` | 12 regional engines plus composite. |
| `ring2_brain/quant/trading_r1/compute/` | `TauricResearch/Trading-R1` `trading_r1/tools/compute` | Fixed income, derivatives, risk, valuation, statistics. |
| `ring2_brain/truth/vibe_trading/grounding/` | `.../agent/src/agent/grounding` | SHA-256 ledger plus evidence/policy/registry support. |
| `ring2_brain/truth/tradingagents/sec_edgar.py` | `TauricResearch/TradingAgents` | `statementKnowledgeTime` PIT guard. |
| `ring2_brain/truth/tradingagents/router.py` | `.../dataflows/router.py` | Multi-vendor data router. |
| `ring2_brain/truth/tradingagents/settlement.py` | `.../memory/settlement.py` | Delayed settlement grading. |
| `ring2_brain/truth/trading_r1/verification/` | `.../trading_r1/runtime/{loop,prompts,evidence}.py` | Numerical re-computation and verification logic. |
| `ring2_brain/cognition/tradingagents/agents/` | `.../tradingagents/agents` | 4 analysts, bull/bear researchers, trader, 3-way risk debaters. |
| `ring2_brain/cognition/tradingagents/graph/` | `.../tradingagents/graph` | Staged committee orchestration. |
| `ring2_brain/mirofish/services/` | `666ghj/MiroFish` `backend/app/services` | Ontology generator, OASIS contagion simulator, and a local JSON graph reader; all Zep Cloud code and `zep_*` modules removed. |
| `ring2_brain/mirofish/models/` | `.../backend/app/models` | Project and task models only. |
| `ring0_vault/kernel/nautilus/crates/execution/src/` | `nautechsystems/nautilus_trader` | Rust ExecEngine. |
| `ring0_vault/kernel/nautilus/crates/risk/src/` | `.../crates/risk/src` | RiskEngine with HALTED/REDUCING paths. |
| `ring0_vault/kernel/nautilus/crates/trading/src/algorithm/` | `.../crates/trading/src/algorithm` | TWAP and Iceberg algorithm module. |
| `ring0_vault/kernel/nautilus/crates/live/src/execution/reconciliation.rs` | `.../crates/live/src/execution` | Live two-way reconciliation. |
| `ring0_vault/kernel/nautilus/python/{execution,risk}/` | `.../python/nautilus_trader` | Python binding stubs/reexports. |
| `ring1_spinal/nautilus_adapters/{binance,okx,bybit,interactive_brokers}/` | `.../python/nautilus_trader/adapters` | Adapter wrappers only. |
| `ring0_vault/ibor/tigerbeetle/python_client/` | `tigerbeetle/tigerbeetle` `src/clients/python/src/tigerbeetle` | Python client bindings. |
| `ring0_vault/ibor/tigerbeetle/samples/{two-phase,two-phase-many}/` | `.../src/clients/python/samples` | Two-phase atomic transfer patterns. |
| `ring0_vault/ibor/tigerbeetle/docs/` | `.../docs/coding/recipes` + `create_transfers.md` | `debits_must_not_exceed_credits` and balance invariants. |
| `ring0_vault/silos/india_brokers/{dhan,zerodha,angel,upstox}/` | `marketcalls/openalgo` `broker/` | India broker adapters. |
| `ring0_vault/silos/key_vault/` | `openalgo/{database,utils}` | Fernet/Argon2 key vault. The Telegram-coupled pepper rotation utility was excluded. |
| `ring0_vault/silos/analyzer_shadow/` | `openalgo/{blueprints,services,utils,database,restx_api,events}` | Analyzer shadow-mode hooks. |
| `ring0_vault/silos/fx_mt5/` | `vnpy/vnpy`, `vnpy/vnpy_mt5`, `vnpy/vnpy_ctp` | MT5/CTP IPC gateway primitives and vn.py core. |
| `ring3_cockpit/ui/openalgo_shell/` | `openalgo/frontend` | React 19 + TS + Vite + Tailwind shell primitives only. |

### 8.2 Harvest gaps and path adjustments

- **Alpaca adapter:** not present in the `nautilus_trader` HEAD. The US silo must use IBKR
  now; Alpaca remains a specified target but has no harvested wrapper. Do not fabricate one.
- **NautilusTrader layout:** upstream moved `ExecEngine` and `RiskEngine` from Python to Rust.
  The Python packages are bindings only, so the implementation was harvested from `crates/`.
  This is a path adjustment, not a feature substitution.
- **Iceberg:** upstream has no separate `iceberg.rs`; Iceberg lives inside
  `crates/trading/src/algorithm/mod.rs`. TWAP is `twap.rs`.
- **OpenAlgo UI:** only the shell was harvested. `src/App.tsx`, route pages, domain components,
  `src/lib/flow`, `src/lib/trading`, CodeMirror/xyflow/Plotly code, and OpenAlgo SDK packages
  were excluded. The 3-tab cockpit is wired in a later step.
- **MiroFish:** `zep_*` services, `graph_builder.py`, report generation, unweighted Twitter/Reddit
  bots, and the Vue SPA were excluded. The remaining simulator services imported Zep Cloud at
  module scope; those imports were replaced with `services/local_graph.py`, which reads a local
  JSON graph (`graph_id` is a filesystem path). No Zep client or `zep_cloud` import remains.
- **TradingAgents:** CLI wizard, single-asset backtester, and raw Yahoo wrappers were excluded.
- **Trading-R1:** `tui/`, `sync/`, plaintext secrets, Telegram/email delivery, and `reportlab`
  were excluded.
- **Vibe-Trading:** chat bots (WhatsApp/WeChat/QQ/DingTalk), Pixel Office, and the CLI slash
  router were excluded.
- **TigerBeetle:** multi-datacenter cluster orchestration was not harvested. Use the installed
  Python package and a single pre-built binary.
- **vn.py:** documentation/examples excluded; MT5 and CTP are modularized addons.
- **OpenAlgo key vault:** `upgrade/rotate_pepper.py` was excluded because it also rewrites
  Telegram bot tables. The Fernet/Argon2 vault itself is in `ring0_vault/silos/key_vault/`.

`_donor_staging/` is reference material only. Runtime code must not import from it. Delete it
only after the corresponding integration tests pass.

---

## 9. Engineering Rules for Agents

1. **Read the JSON first.** Before adding a module, locate the governing section
   (`sec_001`..`sec_018`). If the JSON does not authorize it, do not build it.
2. **Respect ring ownership.** Ring 2 never imports Ring 0 signing code. Ring 3 never imports
   broker adapters. Ring 1 never imports an LLM client.
3. **No floating-point money.** IBOR amounts are 128-bit integers. Use explicit currency and
   tax-lot fields.
4. **No ungrounded numbers.** Any numeric claim in a thesis, card, or proposal must carry a
   SHA-256 commitment and a resolvable source fact.
5. **No lookahead.** Filter by `knowledgeTime <= asOfTime`, including SEC filings and
   restatements.
6. **No cross-border wires.** Capital stays inside its silo. The in-silo ratchet is 35%
   compounder plus 15% reserve.
7. **No synchronous LLM in the hot path.** System 1 writes a 60-second lease; Ring 1 reads
   shared memory.
8. **No unbounded queues.** NATS critical streams require idempotency keys and dead-letter
   handling.
9. **No speculative dependencies.** The approved stack is Python 3.12 + uvloop, Rust/Cython,
   FastAPI, React 19 + TypeScript + Vite + Tailwind, NATS JetStream, DuckDB + Parquet,
   TigerBeetle, PostgreSQL 16, NautilusTrader, Ollama (Qwen-2.5-3B/Llama-3.2-3B),
   Gemini 2.5 Flash-Lite, Groq, DeepSeek-R1, Claude Sonnet/Opus, and OpenRouter. Do not add a
   new framework, database, broker, or model provider without a spec change.
10. **UTC everywhere.** Logs, partitions, leases, Alpha Forge schedules, and settlement use UTC.
11. **Never enable live trading in a test.** Paper mode is the default. Live mode requires an
    explicit human action through `POST /api/v2/control/brokers`.
12. **Keep the air-gap auditable.** Any new outbound call must be listed with its ring, credential
    source, and kill-switch behavior.

---

## 10. Verification Gates

Before any phase is declared complete:

- `specs/` contains TLA+ models for `KillSwitch`, `OrderLifecycle`, `Outbox`, and
  `Reconciliation`.
- TLA+ invariants hold: `GlobalHalt => active leases == 0 and no new orders`;
  no orphaned orders; `AtMostOnceExecution`; `EventualIBORConvergence`, with `>0.01%` drift
  halting the silo.
- NautilusTrader property tests and TigerBeetle VOPR pass.
- Latency harness meets the `sec_010` / `sec_013` budgets on pinned cores.
- The 98% split-conformal gate reports `Net_LB > 0 bps` on the 200-trade residual window.
- The 30-trade L2 shadow twin completes before any live graduation.
- Ring 2 audit proves no broker credentials and no exchange sockets.
- A kill-switch drill disarms all leases in `<1.0 ms`.

Step 1 is a harvest and constitution step only. It does not authorize live orders, MCP,
plugins, skills, or the Phase 4 Alpha Forge.

---

## 11. Current Phase Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Sovereign Kernel & Truth Layer: ring isolation, SHM SeqLock, 480+ factors, DCF/Comps, SHA-256 ledger, TigerBeetle + PG16 IBOR | In progress - skeleton and donor harvest complete |
| 2 | Bicameral Brain & Conformal Gate: 0.14 ms scalper, 0.1 ms breaker, MiroFish, committee, 30-trade shadow twin, 98% gate | Not started |
| 3 | Multi-Silo OMS & Copula Shield: all silos, maker-peg/TWAP, copula shield, ratchet | Not started |
| 4 | Nocturnal Alpha Forge & Kernel Bypass: 00:00 UTC walk-forward, Purged K-Fold, Rust/DPDK sub-20us, options tail hedge | Deferred until Main Core complete |

**Main Core = Phases 1-3.** Do not start Phase 4, MCP, dynamic plugins, or Skills before the
Main Core is complete and verified.
