# NIOS — Four-Ring Sovereign Trading System

NIOS is a four-ring, air-gapped, latency-tiered trading system. Deterministic execution lives
in Ring 0 and Ring 1; slow probabilistic cognition lives in Ring 2; a human operator holds the
controls in Ring 3. The rings exchange bounded, typed contracts and never share mutable state.

> **LLMs may reason about the market. LLMs may never sit in the microsecond execution loop.**

The binding specification is `NIOS_Core_architecture.json` (`sec_001`–`sec_018`) and the
operating constitution is `AGENTS.md`. If code, a prompt, or a convenience shortcut conflicts
with the JSON, the JSON wins.

---

## Quick start

### One click

| Platform | Command |
|---|---|
| Windows | `start_nios.bat` |
| Linux / macOS / WSL | `./start_nios.sh` |

On first use on Linux/macOS, make the script executable: `chmod +x start_nios.sh`.

Both scripts resolve a Python interpreter, verify it can import the runtime, confirm the port
is bindable, run the **17-gate release smoke test**, and only launch the unified server if every
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
origin. Interpreter discovery order: `NIOS_PYTHON` → `NIOS_VENV` → `.venv` → `venv` →
pre-provisioned dev venv → `py -3.12` / `python3.12`.

---

## The four rings

| Ring | Path | Responsibility | Hard boundary |
|---|---|---|---|
| **0 — Sovereign Vault** | `ring0_vault/` | Verifies SHA-256 digests, enforces whitelists and notional caps, runs the 98% split-conformal gate and the cross-silo tail-copula shield, stages two-phase TigerBeetle transfers, signs orders, reconciles every 60 s, enforces ADR-009 loss stops. | The only ring holding broker credentials or signing power. |
| **1 — Spinal Reflex** | `ring1_spinal/` | Deterministic zero-GC hot path. Dual-vendor feed normalization, executed-OFI gate, cancel-to-fill gate, 0.1 ms volatility breaker. | Holds short-TTL leases only. Cannot sign, settle or write the IBOR. |
| **2 — Bicameral Brain** | `ring2_brain/` | 490-factor zoo and valuation lab, SHA-256 grounding ledger, SEC EDGAR point-in-time guard, MiroFish contagion cascade, System 1 lease writer and the System 2 investment committee. | No broker keys, no exchange sockets, no order signing. |
| **3 — Glass Cockpit** | `ring3_cockpit/` | FastAPI REST/SSE control plane, global kill switch, the React 19 three-tab cockpit, and a read-only MCP bridge. | Read-only for market state; may only write the four control endpoints. |
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

Ring 3 surfaces both on the **Contagion & Lease X-Ray** tab and the **Control Room** tab.

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

**202 tests · 0 failures · 0 warnings.** A warning is treated as a defect: `pyproject.toml`
sets `filterwarnings = ["error"]` and exempts only four named third-party donor modules.

```bash
# Windows
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
python -m pytest tests -q

# Linux / WSL
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests -q
```

| File | Covers |
|---|---|
| `test_step2_core_spine.py` | SHM SeqLock, NATS bus, bitemporal lake |
| `test_step3_truth_and_quant.py` | 490-factor zoo, scan SLA, grounding ledger, valuation |
| `test_step4_ring2_cognition.py` | MiroFish cascade, System 1/2 switchboard |
| `test_step5_ring1_spinal.py` | Tier-0 latency + throughput, OFI/breaker gates |
| `test_step6_ring0_vault.py` | Conformal gate, firewall, signing, ADR-009 |
| `test_step7_cockpit_and_e2e.py` | Cockpit REST/SSE, kill switch, full-ring E2E |
| `test_step8_forge_mcp_and_deploy.py` | Alpha Forge, MCP air-gap, deployment |
| `test_phase2_step1…step4` | Donor activation, silo adapters, feeds/LLM pool, TLA+/VOPR/hedger |
| `test_phase2_step4_formal_vopr_and_hedger.py` | Also holds the **final release certification** gate (compiled `dist/` served at `/`) |

With `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `pytest` is the only plugin loaded, so the result is
independent of whatever happens to be installed in the interpreter.

---

## Operator endpoints

The cockpit is read-only for market state. These are the complete write surfaces (`sec_016`).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Compiled React 19 cockpit (`dist/`) |
| `GET` | `/api/v2/cockpit/health` | Ring status and version |
| `GET` | `/api/v2/cockpit/state` | Full hydration snapshot (silos, leases, contagion, LLM, proofs) |
| `GET` | `/api/v2/cockpit/formal-proofs?refresh=` | TLA+/VOPR/backend/hedger telemetry; `refresh=true` re-runs BFS + VOPR |
| `GET` | `/api/v2/cockpit/stream` | SSE cockpit frames at 60 FPS |
| `POST` | `/api/v2/control/brokers` | Paper/Live hot-swap, cost floors, allocation, loss stop |
| `POST` | `/api/v2/control/llms` | LLM provider/model/budget; API keys are write-only |
| `POST` | `/api/v2/control/killswitch` | Global halt; disarms leases and flattens within 1.0 ms |
| `GET`/`POST` | `/api/v2/control/extensions` | Skills, plugins, MCP tools, Alpha Forge audit |
| `GET` | `/api/v2/mcp/tools` | Read-only MCP tool catalogue + air-gap audit |
| `POST` | `/api/v2/mcp/call` · `/api/v2/mcp` · `/api/v2/mcp/rpc` | MCP invocation (JSON-RPC + SSE) |

### Cockpit tabs

1. **Live Cards** — plain-English SSE cards with proof telemetry.
2. **Contagion & Lease X-Ray** — formal verification / VOPR badge, 60 s lease table, MiroFish
   archetypes, SHA-256 grounding ledger, TacticalVault tail-hedger card.
3. **Control Room** — token budget, 12/12 venue adapter matrix, broker hot-swap, LLM
   switchboard, extensions.

---

## Repository map

```text
core/                      shared substrate + orchestrator
ring0_vault/               kernel/ ibor/ silos/
ring1_spinal/              zero-GC hot path + feed adapters
ring2_brain/               quant/ truth/ mirofish/ cognition/
ring3_cockpit/             gateway/ ui/openalgo_shell/
specs/                     TLA+ models: KillSwitch, OrderLifecycle, Outbox, Reconciliation
tests/                     11 suites, 202 tests
scripts/run_nios.py        smoke test + unified server
start_nios.bat / .sh       1-click launch
```

`_donor_staging/` is reference material only; runtime code must never import from it.

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
