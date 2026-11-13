# NIOS Production Operator Runbook

**Document ID:** NIOS-OPS-2026-V3
**Release tag:** `v3.1.0-V1` (supersedes `v3.0.0-PROD`, which stays frozen)
**Branch:** `V1`
**Runtime platform version:** `3.0.0-PROD` (`GET /api/v2/cockpit/health` → `version`)
**Governing specification:** `NIOS_Core_architecture.json` (`sec_001`–`sec_018`)
**Operating constitution:** `AGENTS.md`

This runbook is the on-call reference for the four-ring NIOS deployment. It assumes the
release smoke test has passed (`python scripts/run_nios.py --smoke-test` → `EXIT=0`) and
that the operator understands the prime directive:

> **LLMs may reason about the market. LLMs may never sit in the microsecond execution loop.**

Every action below is either **read-only** or an **explicit control endpoint**. Paper mode is
the default in every silo; live capital requires a deliberate operator action.

---

## SOP 1 — Boot & Deployment Modes

### 1.1 Single-command launcher (default)

| Platform | Command |
|---|---|
| Windows | `start_nios.bat` |
| Linux / macOS / WSL | `./start_nios.sh` |

The launcher resolves the interpreter (`NIOS_PYTHON` → `NIOS_VENV` → `.venv` → `venv` →
`py -3.12`), verifies the runtime imports, confirms the port is bindable, runs the release
smoke test, and only then serves the unified app. A failed precondition never serves a
non-certified build.

```bash
start_nios.bat                 # smoke test, then serve http://127.0.0.1:8000
start_nios.bat --skip-smoke    # serve only (never for a release gate)
python scripts/run_nios.py --host 127.0.0.1 --port 8000
```

Health check: `GET /api/v2/cockpit/health` → `ok=true`, `status=ARMED`, `version=3.0.0-PROD`.

### 1.2 Four-ring OS multi-process mode (`sec_004` process isolation)

```bash
python scripts/run_nios.py --multiprocess
python scripts/run_nios.py --multiprocess --silo CRYPTO_BINANCE --feed-mode PAPER_AUTOPILOT
python scripts/run_nios.py --multiprocess --seconds 300   # headless supervisor window
```

This starts **four distinct OS processes** (Ring 0 vault, Ring 1 spinal, Ring 2 brain,
Ring 3 cockpit). Verify the topology:

```bash
curl.exe -s http://127.0.0.1:8000/api/v2/control/supervisor
```

`distinct_os_processes` must be `4` and every ring PID must be different. The cockpit never
merges rings into one process in this mode.

### 1.3 Docker Compose topology (network-segmented)

```bash
docker compose up --build
```

| Service | Networks | Contract |
|---|---|---|
| `ring0-vault` | `ring0-net` | Only ring holding keys, signers and the IBOR |
| `ring1-spinal` | `ring0-net`, `ring1-net` | Pinned CPUs, large `/dev/shm` |
| `ring2-brain` | `ring1-net`, `ring2-net` | No route to Ring 0 / broker sockets |
| `ring3-cockpit` | `ring2-net`, `ring3-net` | Public `8000/tcp`, no broker network |

Stop cleanly with `docker compose down` (state lives in the `ring0-state` volume). Never
publish the Ring 0 or Ring 1 networks to the host.

### 1.4 Shutdown

`Ctrl+C` / `docker compose down` is graceful. The supervisor stops workers, the paper
autopilot task is cancelled, and `CrashAtomicOutbox` leaves any in-flight reservation in a
recoverable state. Do **not** kill a live process mid-order; use the kill switch (SOP 2).

---

## SOP 2 — Emergency Incident Response

### 2.1 Global kill switch (`<1.0 ms` budget)

```bash
curl.exe -s -X POST http://127.0.0.1:8000/api/v2/control/killswitch \
  -H "Content-Type: application/json" \
  -d "{\"reason\":\"operator emergency halt\",\"flatten\":true,\"confirm\":true}"
```

What happens inside the measured window:

1. The global kill latch in Tier-0 shared memory flips (atomic, lock-free).
2. Every Ring 1 lease is disarmed; new entries stop immediately.
3. Resting entry orders are cancelled and their outbox reservations voided.
4. `ADR-009` reduce-only flatten proposals are signed for residual exposure.
5. The report contains `latency_ns`; it must be `< 1,000,000`. A breach is a P1 defect.

`confirm=true` is mandatory — the endpoint returns `400` otherwise. Never "retry until it
works": a second call while halted is a no-op and the first report is the evidence.

### 2.2 ADR-009 reduce-only and vault stops

A silo that breaches its daily loss stop is locked and switched to `REDUCING`; only
reduce-only orders may be signed. Verify the state:

```bash
curl.exe -s http://127.0.0.1:8000/api/v2/cockpit/state
```

Look for `silos.<SILO>.trading_state` (`REDUCING`/`HALTED`) and `max_daily_loss_micros`.

### 2.3 Re-arming after a halt

A human must reset an ADR-009 stop (`sec_004` Tier 3). The only path back to `ARMED`:

```bash
curl.exe -s -X POST http://127.0.0.1:8000/api/v2/control/rearm \
  -H "Content-Type: application/json" \
  -d "{\"reason\":\"incident reviewed\",\"operator\":\"oncall.name\"}"
```

This clears the global latch, unfreezes every silo, sets trading state back to `ACTIVE`,
and lets the paper autopilot resume on its next tick — no restart. Confirm
`kill_latch_hot=false` and `status=ARMED` in the reply.

### 2.4 Unfreezing a silo after a reconciliation drift halt

`LiveReconciler` reconciles every 60 s. Drift `>0.01%` freezes the affected silo
(`SiloGuard`), which blocks new entries but keeps the ledger intact.

1. Read the drift evidence: `GET /api/v2/cockpit/state` → `silos.<SILO>.frozen` and the
   reconciliation report in the Ring 1/Brain tabs.
2. Reconcile the ledger against the venue statement **before** unfreezing. A drift halt is
   a data-integrity alarm, not a nuisance.
3. Once the cause is understood and corrected, re-arm (2.3) or unfreeze via the supervisor
   control surface. Never edit the ledger to silence a drift.

Escalate to the release owner if drift persists after one correction cycle.

---

## SOP 3 — Crash Recovery & Soak Verification

### 3.1 Accelerated soak (release gate)

```bash
python scripts/soak_test_harness.py --cycles 10000 --audit-memory
python scripts/soak_test_harness.py --cycles 2000 --silos CRYPTO_BINANCE,INDIA_ZERODHA_DHAN --json
```

The harness drives the four silos through the full Ring 0/Ring 1 path, samples the Python
heap and handle counts, and fails on a positive heap slope beyond `--leak-tolerance-mb`
(default `0.005` MB/cycle) or on SHM/handle growth. `--json` emits the machine report used
by CI. A soak is not a substitute for the chaos test; run both before a release.

### 3.2 Cold-start after power loss

The recovery contract is `CrashAtomicOutbox` + `IBORLedger`:

1. **Orphaned reservations** — every pending transfer written before the crash is replayed
   from the outbox. A reservation whose order never reached the venue is voided; a filled
   order is committed. `AtMostOnceExecution` still holds because the idempotency key is the
   outbox record.
2. **Double-entry conservation** — replay re-derives debits and credits from the journal.
   Sum of debits must equal sum of credits per currency; a mismatch halts the silo instead
   of guessing.
3. **Stale leases** — leases are TTL-bound in shared memory. After a restart nothing is
   armed; Ring 1 refuses to initiate until System 1 publishes a fresh 60 s lease.
4. **Kill latch** — a latched halt survives the restart. The platform comes up `HALTED`
   and requires the explicit re-arm in SOP 2.3.

Verification commands:

```bash
python scripts/run_nios.py --smoke-test          # EXIT=0 required, 22 rows
python -m pytest tests -q                        # 278 tests / 18 suites, 0 failures, 0 warnings
python scripts/preflight_golive.py --strict      # go-live readiness certificate, 22/22
python scripts/preflight_golive.py --fast        # replay smoke receipt (interactive), 22/22
```

Release seal for `v3.1.0-V1`: `pytest tests -q` must report **278 passed, 0 failed, 0 warnings**
and `python scripts/preflight_golive.py --fast` must report **22/22 PASS, EXIT=0**. Both are
prerequisites for re-tagging; neither may be waived.

### 3.3 Chaos / crash injection

```bash
python -c "from core.chaos_recovery import *; print('chaos module ready')"
```

The deterministic chaos engine SIGKILLs a worker between outbox stages and asserts:
no orphaned orders, at-most-once execution, `debit == credit`, and fail-closed stale
leases. Any assertion failure is a release blocker.

---

## SOP 4 — Zero-Downtime Key Rotation & Live Silo Onboarding

### 4.1 Rotate Ring 0 Fernet + Ed25519 keys

```bash
curl.exe -s http://127.0.0.1:8000/api/v2/control/vault/status

curl.exe -s -X POST http://127.0.0.1:8000/api/v2/control/vault/rotate-keys \
  -H "Content-Type: application/json" \
  -d "{\"operator\":\"oncall.name\",\"reason\":\"scheduled rotation\"}"
```

The rotation is atomic and zero-downtime:

* a fresh Fernet master key is generated for the LLM store and for the broker store;
* a fresh Ed25519 order-signing keypair is generated and `key_version` increments;
* every stored LLM credential and broker venue credential is decrypted under the old key
  and re-encrypted under the new one (temp file + `os.replace`, `0600`);
* the previous Ed25519 public key is archived in the read-only verifier ring so historical
  signed orders still verify;
* a short-lived journal makes a crash mid-rotation recoverable on the next call.

The reply is proof-only — `key_version`, `ed25519_public_fingerprint`,
`reencrypted_llm_keys_count`, `reencrypted_broker_venues_count`,
`credentials_zero_loss`, `dry_run_verified`, `historical_signature_verified` and the OS
tuning summary. **No raw Fernet key, private Ed25519 seed or secret token is ever returned.**

Verify afterwards: `GET /api/v2/control/vault/status` must show the new `key_version`, the
same `***last4` masks in `GET /api/v2/control/brokers/credentials`, and
`rotation_pending_recovery=false`. Rotate during a quiet window anyway: the operation is
fast but it holds the vault write lock.

### 4.2 Onboard masked venue credentials

```bash
curl.exe -s -X POST http://127.0.0.1:8000/api/v2/control/brokers/credentials \
  -H "Content-Type: application/json" \
  -d "{\"silo\":\"CRYPTO_BINANCE\",\"venue\":\"binance\",\"api_key\":\"...\",\"api_secret\":\"...\",\"sandbox\":true}"
```

Ring 0 encrypts both values, builds the venue-native order payload, validates its field
schema, signs a SHA-256 digest with Ed25519 and proves the Fernet envelope round-trips. No
socket is opened and no order is placed. The response carries only `***last4` plus the
dry-run receipt. Repeat per venue (`binance`, `dhan`, `ibkr`, `oanda`, `mt5`).

### 4.3 Transition a silo from `PAPER` to `LIVE`

Preconditions: the venue credential is configured (`dry_run_verified=true`), every adapter
in the silo is `ready`, the kill latch is clear, **and the 22-gate pre-flight certificate
reports `failed_gates == 0`**.

```bash
curl.exe -s http://127.0.0.1:8000/api/v2/control/preflight

curl.exe -s -X POST http://127.0.0.1:8000/api/v2/control/brokers \
  -H "Content-Type: application/json" \
  -d "{\"silo\":\"CRYPTO_BINANCE\",\"mode\":\"LIVE\",\"confirm_live\":true,\"operator\":\"oncall.name\"}"
```

`confirm_live=true` is mandatory; without it the request is rejected with `403`. With it,
the gateway re-checks the certificate and refuses the transition with `409`
`PRE_FLIGHT_GATE_BLOCKED` — naming every failed gate id — if any constitutional invariant
is broken. The change applies in memory with zero worker restart. Reverse with
`"mode":"PAPER"` (always allowed). Paper is the default after every restart — live is never
sticky.

### 4.4 22-gate go-live certificate

```bash
curl.exe -s http://127.0.0.1:8000/api/v2/control/preflight            # fast path
curl.exe -s -X POST http://127.0.0.1:8000/api/v2/control/preflight \
  -H "Content-Type: application/json" -d "{\"full_smoke\":true}"      # full re-run
python scripts/preflight_golive.py --strict                           # CLI, release gate
```

The fast path replays a short-lived in-process certificate and the persisted receipt of the
last certified 22-row smoke suite for the current commit, so the cockpit stays responsive.
`full_smoke=true` (or `--fast` omitted on the CLI) re-runs every gate from scratch. The reply
carries `ready_for_live_cutover`, `passed_gates` / `total_gates`, `failed_gates`,
`failed_gate_ids`, the per-gate checklist and a deterministic 64-character
`certificate_sha256`. Treat a `FAIL` row as a release blocker: never re-tag, never widen a
gate, and never hand-edit a silo to `LIVE` around it.

### 4.5 Forge + tail-hedge drill (Ring 2 proposes, Ring 0 signs)

```bash
curl.exe -s -X POST http://127.0.0.1:8000/api/v2/control/supervisor/forge-hedge \
  -H "Content-Type: application/json" -d "{\"operator\":\"oncall.name\"}"
```

One closed-loop cycle: the Nightly Alpha Forge audits champions, demotes anything under
`+0.50 bps` and disarms its Ring 1 lease, breeds challengers, runs the three-tick MiroFish
cascade, and the TacticalVault tail-hedger prices a grounded protective put. Ring 0 verifies
the proposal and returns an Ed25519 `TACTICAL_SWING` insurance receipt. In
`--multiprocess` mode the command travels to the Ring 2 worker and the signature comes back
from the Ring 0 worker (`mode=cross_process`); single-process mode runs the same ordering
in-process (`mode=single_process`). The reply reports `retired_champions`,
`disarmed_leases`, `genomes_evaluated` and `ring0_signature.verified` — a rejection is data,
not an error: fix the cause, do not bypass the vault cap.

---

## SOP 5 — Prometheus / OpenTelemetry Monitoring & OS Tuning

### 5.1 Scrape targets

| Path | Format | Use |
|---|---|---|
| `/metrics` | Prometheus text | Stock scrape alias |
| `/api/v2/metrics` | Prometheus / OpenMetrics | Primary scrape target |
| `/api/v2/metrics/json` | JSON | Cockpit telemetry chip, ad-hoc probes |

Required alert families: `nios_tick_latency_seconds` (p99 `<= 0.00014`), 
`nios_ring_heartbeat_age_ms` (must stay fresh), `nios_conformal_q98_bps`,
`nios_outbox_reservations_total` (pending must not grow), `nios_token_spend_usd_total`
against the `3.10 USD` daily cap, `nios_active_leases`, `nios_silo_nav_micros`.

```bash
curl.exe -s http://127.0.0.1:8000/metrics | Select-String "nios_tick_latency_seconds"
```

A `DEGRADED` telemetry chip or a stale heartbeat is a P2 until proven otherwise.

### 5.2 OS and socket low-latency audit

```bash
python scripts/tune_os_performance.py --check         # read-only, no admin/root needed
python scripts/tune_os_performance.py --check --json  # machine report for CI
python scripts/tune_os_performance.py --apply         # apply what this process may change
```

The audit proves, with kernel read-back values:

* `TCP_NODELAY=1` (Nagle disabled), `SO_KEEPALIVE=1` and 1 MiB receive/send buffers on a
  real loopback TCP pair, plus conformance of the HTTP/2 LLM path and the Ring 1 venue
  WebSocket multiplexer;
* **Windows 11** — multimedia timer resolution (`NtQueryTimerResolution`,
  `timeBeginPeriod(1)`), priority class (`ABOVE_NORMAL`/`HIGH`), CPU affinity mask
  (`SetProcessAffinityMask`) and Tier-0 shared-memory capacity;
* **Linux / WSL** — `net.core.rmem_max=16777216`, `net.core.wmem_max=16777216`,
  `net.core.busy_poll=50`, `net.core.busy_read=50`, `/dev/shm` capacity, transparent
  hugepage mode and `sched_setaffinity` core isolation.

Exit code `0` means the probe ran with `0` errors. `RECOMMENDED` rows print the exact
PowerShell / `sysctl` command; apply them only on the trading host, then re-run `--check`
until every latency-critical row is `APPLIED`. Never disable the audit to silence a
`RECOMMENDED` row.

### 5.3 Canonical endpoint reference

Every route below is registered on the Ring 3 gateway. `sec_016` still holds: market state is
read-only and the only writes are the explicit control surfaces in this table.

#### 5.3.1 Cockpit state (read-only)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v2/cockpit/health` | Ring status, version, configured venues |
| `GET` | `/api/v2/cockpit/state` | Full hydration snapshot |
| `GET` | `/api/v2/cockpit/stream` | SSE cockpit frames at 60 FPS |
| `GET` | `/api/v2/cockpit/formal-proofs?refresh=` | TLA+ / VOPR / backend telemetry |

#### 5.3.2 Page telemetry — 7 read-only endpoints

One endpoint per non-dashboard page. Each returns the page header, ring ownership, live organ
counts and the row set the SPA renders. None of them mutates state, opens a socket, or reads a
credential.

| Method | Path | Backing surface |
|---|---|---|
| `GET` | `/api/v2/cockpit/pages` | 9-page catalogue, alias map, platform-wide counts |
| `GET` | `/api/v2/cockpit/orders` | SmartOMS blotter, Maker-Peg/TWAP slicers, outbox |
| `GET` | `/api/v2/cockpit/analyzer` | 30-fill L2 shadow nursery, adverse selection |
| `GET` | `/api/v2/cockpit/quant` | 490-factor scan, DCF / Black-Scholes, loaders |
| `GET` | `/api/v2/cockpit/forge` | Alpha Forge leaderboard, plugins, SKILL.md catalog |
| `GET` | `/api/v2/cockpit/ledger` | Double-entry journal, 35/15 ratchet, TigerBeetle + PG16 |
| `GET` | `/api/v2/cockpit/mcp` | Read-only MCP catalogue + air-gap audit |
| `GET` | `/api/v2/cockpit/skills/{skill_name}` | One SKILL.md document as structured Markdown |

`/dashboard`, `/brain`, `/verification` and `/control` have no dedicated page endpoint — they
hydrate from `GET /api/v2/cockpit/state` plus `GET /api/v2/cockpit/formal-proofs`.

#### 5.3.3 Broker, feed, LLM and vault control

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v2/control/brokers` | Paper/Live, fees, allocation, loss stop |
| `GET`/`POST` | `/api/v2/control/brokers/credentials` | Masked inventory / write-only handshake |
| `GET` | `/api/v2/control/brokers/testnet` | Sandbox bridge state |
| `POST` | `/api/v2/control/brokers/testnet-order` | Ring 0 sandbox order dispatch |
| `GET`/`POST` | `/api/v2/control/feeds` | Feed mode hot-swap |
| `POST` | `/api/v2/control/llms` | LLM switchboard (write-only keys) |
| `POST` | `/api/v2/control/llms/probe` | Local probe under the 250 ms cap |
| `POST` | `/api/v2/control/killswitch` | Global halt + ADR-009 flatten |
| `POST` | `/api/v2/control/rearm` | Human reset back to `ARMED` |
| `GET`/`POST` | `/api/v2/control/supervisor` | 4-ring process supervision |
| `POST` | `/api/v2/control/supervisor/forge-hedge` | Closed-loop forge + Ring 0 signed tail hedge |
| `GET`/`POST` | `/api/v2/control/preflight` | 22-gate go-live readiness certificate |
| `GET`/`POST` | `/api/v2/control/extensions` | Skills / plugins / MCP / Forge |
| `POST` | `/api/v2/control/vault/rotate-keys` | Zero-downtime Fernet + Ed25519 rotation |
| `GET` | `/api/v2/control/vault/status` | Key version, fingerprint, OS tuning |

#### 5.3.4 Workstation control — 12 interactive operator actions

These are the Step 5–6 additions. Every one is explicit, audited and reversible; none of them
places a live order, opens a venue socket, or unlocks a silo.

| Method | Path | Effect |
|---|---|---|
| `POST` | `/api/v2/control/orders/paper` | Grounded paper order → outbox reservation → `SmartOMS` (Maker-Peg / TWAP-Iceberg) → 35/15/50 ratchet. No venue field exists. |
| `POST` | `/api/v2/control/orders/cancel` | Cancel one `order_id` or `ALL`, voiding the matching pending outbox reservation. |
| `POST` | `/api/v2/control/analyzer/shadow-burst` | Drive `rounds` paper autopilot ticks to approach the 30-fill shadow graduation threshold. |
| `POST` | `/api/v2/control/quant/price-option` | Black-Scholes price + Greeks, sealed in a SHA-256 `GroundedFactPacket`. |
| `POST` | `/api/v2/control/quant/scan` | 490-factor scan returning the top `k` candidates. |
| `POST` | `/api/v2/control/ledger/reconcile` | `LiveReconciler.reconcile_all` + double-entry conservation proof. |
| `POST` | `/api/v2/control/verification/chaos-drill` | Deterministic SIGKILL mid-outbox drill; lease sweep runs on an isolated SHM segment. |
| `POST` | `/api/v2/control/verification/vopr` | Seeded VOPR fault campaign; same seed ⇒ same histogram. |
| `POST` | `/api/v2/control/verification/os-tune` | Apply + audit OS low latency for the serving process; privileged rows stay `RECOMMENDED`. |
| `POST` | `/api/v2/control/brain/mirofish-shock` | One five-archetype, three-tick contagion cascade with a reproducible digest. |
| `POST` | `/api/v2/control/brain/committee-debate` | One grounded System 2 debate turn; `symbol=AUTO` debates the top scan candidate. |
| `POST` | `/api/v2/control/forge/evaluate-challenger` | Nursery admission gate: `Deflated Sharpe >= 1.50` **and** `shadow_fills >= 30`, both read from the forge. |

`/verification/*` calls are non-destructive by construction: the chaos drill uses an isolated
shared-memory segment so a drill never disarms the live Ring 1 lease table, and the VOPR
campaign is a pure simulator. `/brain/*` and `/forge/*` are Ring 2 surfaces that emit proposals
and verdicts only — Ring 0 alone signs.

#### 5.3.5 MCP bridge and metrics

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v2/mcp/tools` | Read-only MCP catalogue + air-gap audit |
| `POST` | `/api/v2/mcp/call` | Invoke one read-only MCP tool |
| `GET` | `/api/v2/metrics` | Prometheus exposition |
| `GET` | `/api/v2/metrics/json` | JSON telemetry |
| `GET` | `/metrics` | Prometheus scrape alias |

---

## SOP 6 — Nine-Page Cockpit Workstation

### 6.1 The nine pages

The cockpit is a single React 19 SPA with nine production pages. `GET /api/v2/cockpit/pages`
returns the authoritative catalogue, including the alias map; a direct navigation or browser
refresh on any route is served by the SPA catch-all and never 404s.

| # | Page | Route | Aliases | Ring | Summary |
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

`/api` and `/metrics` are excluded from the catch-all, so an unknown API path still returns a
real `404` instead of the SPA shell.

### 6.2 Which page writes what

| Page | Read path | Write path |
|---|---|---|
| `/dashboard` | `state`, `stream` | — |
| `/orders` | `cockpit/orders` | `orders/paper`, `orders/cancel` |
| `/analyzer` | `cockpit/analyzer` | `analyzer/shadow-burst` |
| `/quant` | `cockpit/quant` | `quant/price-option`, `quant/scan` |
| `/brain` | `state`, `formal-proofs` | `brain/mirofish-shock`, `brain/committee-debate` |
| `/forge` | `cockpit/forge`, `skills/{name}` | `forge/evaluate-challenger` |
| `/ledger` | `cockpit/ledger` | `ledger/reconcile` |
| `/verification` | `formal-proofs` | `verification/chaos-drill`, `/vopr`, `/os-tune` |
| `/control` | `cockpit/mcp`, `control/preflight` | `brokers`, `llms`, `killswitch`, `vault/*` |

### 6.3 Non-destructive 9-page burn-in sweep

Run this before declaring a documentation or UI release sealed. It never places a live order
and never mutates a credential.

```bash
# 1. Pick a free port (never fight a daemon for 8000)
python scripts/nios_port_check.py --scan 8000 8010

# 2. Boot the unified server on the port it printed
python scripts/run_nios.py --host 127.0.0.1 --port <PORT>

# 3. Sweep all 9 SPA routes + 4 aliases (expect 200 + index.html on every one)
#    /dashboard /orders /analyzer /quant /brain /forge /ledger /verification /control
#    / /positions /xray /mcp

# 4. Sweep the 7 page-telemetry endpoints + skills document
#    /api/v2/cockpit/pages /orders /analyzer /quant /forge /ledger /mcp
#    /api/v2/cockpit/skills/<skill_name>

# 5. Sweep the 12 workstation controls with their documented payloads (expect 200)

# 6. Shut down cleanly; `git status --porcelain` must be empty afterwards.
```

Acceptance: **9/9 routes**, **7/7 page endpoints**, **12/12 controls** at `200 OK`, then a clean
`Ctrl+C` and an empty `git status --porcelain`. Any non-200 is a release blocker — do not retry
around it, read the response body.

### 6.4 Reference payloads

```bash
POST /api/v2/control/orders/paper
{"silo":"CRYPTO_BINANCE","symbol":"BTC-USDT","side":"BUY","quantity":0.05,
 "order_style":"MAKER_PEG","expected_gross_bps":24.0,"fill_immediately":true}

POST /api/v2/control/orders/cancel    {"order_id":"ALL"}
POST /api/v2/control/analyzer/shadow-burst {"silo":"CRYPTO_BINANCE","rounds":10}
POST /api/v2/control/quant/price-option
{"symbol":"BTC-OPT","spot":50000,"strike":46500,"volatility":0.62,
 "days_to_expiry":30,"risk_free_rate":0.05,"option_type":"PUT"}
POST /api/v2/control/quant/scan       {"top_k":15}
POST /api/v2/control/ledger/reconcile {"silos":null}
POST /api/v2/control/verification/chaos-drill {"iterations":120,"hold_ms":120,"seed":11}
POST /api/v2/control/verification/vopr {"seed":7,"steps":250}
POST /api/v2/control/verification/os-tune {"apply":true,"ring":"ring1"}
POST /api/v2/control/brain/mirofish-shock
{"symbol":"BTCUSDT","shock_bps":-45,"sentiment_tag":"panic","volatility_ratio":2.0,
 "silo":"CRYPTO_BINANCE"}
POST /api/v2/control/brain/committee-debate {"silo":"US_IBKR","symbol":"AUTO","spread_bps":3.0}
POST /api/v2/control/forge/evaluate-challenger
{"plugin_name":"challenger_alpha_7","candidate_sharpe":1.85,"shadow_fills":30,"silo":"US_IBKR"}
```

`committee-debate` (and `orders/paper`) reject an unknown `silo` with `422`; use a real silo id
(`CRYPTO_BINANCE`, `INDIA_ZERODHA_DHAN`, `US_IBKR`, `FX_MT5`), not the short model default.

A rejected challenger, a chaotic drill that reports a violation, or a committee verdict of
`NO_TRADE` is **data, not an error**: fix the cause or accept the abstention. Never widen a
threshold or bypass Ring 0 to turn a red row green.

---

## Escalation checklist

1. Capture evidence before changing state: kill-switch report, `cockpit/state`,
   `/api/v2/metrics`, `vault/status`, recent logs.
2. Freeze scope: paper mode first, reduce-only if any capital is exposed.
3. One change per step; re-read the state endpoint after each.
4. Never disable the kill switch, the conformal gate, the air-gap, or the OS audit to make
   an alert go away.
5. Record the incident, the exact endpoint bodies used, and the recovery verification in the
   release log before re-arming live capital.
