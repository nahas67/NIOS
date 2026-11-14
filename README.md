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
is bindable, run the **22-gate release smoke test**, and only launch the unified server if every
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
origin. The SPA exposes **nine pages** — `/dashboard`, `/orders`, `/analyzer`, `/quant`,
`/brain`, `/forge`, `/ledger`, `/verification`, `/control` — plus the aliases `/` (dashboard),
`/positions` (orders), `/xray` (brain) and `/mcp` (control). Interpreter discovery order:
`NIOS_PYTHON` → `NIOS_VENV` → `.venv` → `venv` → pre-provisioned dev venv → `py -3.12` /
`python3.12`.

---

## The four rings

| Ring | Path | Responsibility | Hard boundary |
|---|---|---|---|
| **0 — Sovereign Vault** | `ring0_vault/` | Verifies SHA-256 digests, enforces whitelists and notional caps, runs the 98% split-conformal gate and the cross-silo tail-copula shield, stages two-phase TigerBeetle transfers, signs orders, reconciles every 60 s, enforces ADR-009 loss stops. | The only ring holding broker credentials or signing power. |
| **1 — Spinal Reflex** | `ring1_spinal/` | Deterministic zero-GC hot path. Dual-vendor feed normalization, executed-OFI gate, cancel-to-fill gate, 0.1 ms volatility breaker. | Holds short-TTL leases only. Cannot sign, settle or write the IBOR. |
| **2 — Bicameral Brain** | `ring2_brain/` | 490-factor zoo and valuation lab, SHA-256 grounding ledger, SEC EDGAR point-in-time guard, MiroFish contagion cascade, System 1 lease writer and the System 2 investment committee. | No broker keys, no exchange sockets, no order signing. |
| **3 — Glass Cockpit** | `ring3_cockpit/` | FastAPI REST/SSE control plane, global kill switch, the React 19 nine-page cockpit, and a read-only MCP bridge. | Read-only for market state; may only write the explicit control endpoints. |
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

**312 tests · 20 suites · 0 failures · 0 warnings.** A warning is treated as a defect:
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
| 12 | `test_phase2_step4_formal_vopr_and_hedger.py` | TLA+/VOPR, tail hedger, Phase 2 certification gate (compiled `dist/` served at `/`) |
| 13 | `test_phase3_step1_multiprocess_and_live_daemons.py` | 4-ring OS supervisor |
| 14 | `test_phase3_step2_live_cutover_and_cockpit.py` | Live cutover, masked credentials |
| 15 | `test_phase3_step3_testnet_supervisor_and_security.py` | Testnet bridge, supervisor, security |
| 16 | `test_phase3_step4_soak_metrics_and_chaos.py` | Soak metrics, chaos drills |
| 17 | `test_phase3_step5_os_tuning_key_rotation_and_runbook.py` | OS/socket auditor, zero-downtime Fernet + Ed25519 rotation, runbook completeness |
| 18 | `test_phase4_final_preflight_and_supervisor_forge.py` | 22-gate pre-flight certificate, supervisor forge/tail-hedge, Phase 4 release seal |
| 19 | `test_v1_1_auth_dual_workspace_and_clean_slate.py` | **V1.1** operator auth, dual workspaces, clean slate, LIVE mutation guard |

With `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `pytest` is the only plugin loaded, so the result is
independent of whatever happens to be installed in the interpreter.

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

## The nine-page cockpit workstation

The Ring 3 cockpit is a React 19 SPA with nine pages. `GET /api/v2/cockpit/pages` is the
authoritative catalogue; the SPA catch-all serves any route (and alias) on direct navigation or
refresh, while `/api/*` and `/metrics` still return a real `404`.

| # | Page | Route | Aliases | Ring | What it shows |
|---|---|---|---|---|---|
| 1 | Dashboard | `/dashboard` | `/` | Ring 3 | 4-silo IBOR vault cards, SSE proof stream, 60 s leases |
| 2 | Orders & Positions | `/orders` | `/positions` | Ring 0 | SmartOMS blotter, Maker-Peg/TWAP slicers, atomic outbox |
| 3 | Shadow Analyzer | `/analyzer` | — | Ring 1 | 30-fill L2 shadow nursery, adverse-selection telemetry |
| 4 | Quant & Valuation Lab | `/quant` | — | Ring 2 | 490-factor scanner, DCF/Black-Scholes, loader readiness |
| 5 | Brain & Contagion X-Ray | `/brain` | `/xray` | Ring 2 | Ring isolation, MiroFish cascade, committee, SHA-256 proofs |
| 6 | Alpha Forge & Plugins | `/forge` | — | Ring 2 | Nocturnal Forge leaderboard, plugins, SKILL.md catalog |
| 7 | IBOR Ledger & Tax | `/ledger` | — | Ring 0 | Double-entry journal, 35/15 ratchet, TigerBeetle + PG16 |
| 8 | Proofs, Chaos & Telemetry | `/verification` | — | Ring 0 | TLA+ BFS proofs, VOPR campaign, OS tuning, metrics |
| 9 | Control Room & MCP | `/control` | `/mcp` | Ring 3 | 22-gate pre-flight, credential vault, LLM switchboard, MCP |

---

## Operator endpoints

The cockpit is read-only for market state. Market-data reads are unauthenticated and
side-effect-free; the only writes are the explicit control surfaces below (`sec_016`).

### Read-only page telemetry

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v2/cockpit/pages` | 9-page catalogue, alias map, platform counts |
| `GET` | `/api/v2/cockpit/orders` | SmartOMS blotter, slicers, outbox pending |
| `GET` | `/api/v2/cockpit/analyzer` | Shadow nursery and adverse-selection telemetry |
| `GET` | `/api/v2/cockpit/quant` | 490-factor scan, valuation, loader readiness |
| `GET` | `/api/v2/cockpit/forge` | Forge leaderboard, plugins, SKILL.md catalog |
| `GET` | `/api/v2/cockpit/ledger` | Double-entry journal, ratchet, TigerBeetle + PG16 |
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
tests/                     312 tests across 20 suites
scripts/run_nios.py        smoke test + unified server
scripts/nios_port_check.py free-port resolver (--scan 8000 8010)
scripts/preflight_golive.py 22-gate go-live readiness certificate (--strict / --fast)
scripts/tune_os_performance.py  OS/socket low-latency audit (--check/--apply/--json)
scripts/soak_test_harness.py    accelerated soak + memory-leak audit
RUNBOOK.md                 production SOPs 1-6
start_nios.bat / .sh       1-click launch
```

`_donor_staging/` is reference material only; runtime code must never import from it.

---

## Release seal — `v3.2.0-V1.1`

| Gate | Command | Required result |
|---|---|---|
| Unit / integration | `pytest tests -q` | **312 passed**, 0 failed, 0 warnings |
| Smoke suite | `python scripts/run_nios.py --smoke-test` | **22/22 rows**, `EXIT=0` |
| Go-live certificate | `python scripts/preflight_golive.py --fast` | **22/22 PASS**, `EXIT=0` |
| Live `V1.1` burn-in | boot on `:8001`, `setup → login → paper state → fund-paper → live state (403) → elevate-live → live state (200) → logout`, shut down | **clean slate on boot**, **`403` before elevation**, **`200` after**, then empty `git status --porcelain` |

The seal asserts nothing new about live capital: every silo still boots `PAPER`, the 22-gate
certificate still governs a LIVE cutover, and `main` (`9a8ac5f` / `v3.0.0-PROD`) and `V1`
(`ff67b5c` / `v3.1.0-V1`) are untouched.

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
