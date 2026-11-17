# NIOS Production Operator Runbook

**Document ID:** NIOS-OPS-2026-V3
**Release tag:** `v3.10.0-V1.9` (supersedes `v3.9.0-V1.8`, `v3.8.0-V1.7`, `v3.7.0-V1.6`, `v3.6.0-V1.5`, `v3.5.0-V1.4`, `v3.4.0-V1.3`, `v3.3.0-V1.2`, `v3.2.0-V1.1`, `v3.1.0-V1` and `v3.0.0-PROD`, all frozen)
**Branch:** `V1.9`
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

## SOP 1B — V1.1 Sovereign Operator, Dual Workspaces & Clean Slate

`V1.1` replaces the shared three-tab cockpit with a **two-tier operator session** and two
physically separated workspaces. Nothing in the UI is reachable without an operator
credential, and no live mutation is reachable with a PAPER-tier session.

### 1B.1 The two tiers

| Tier | How it is minted | Lifetime | Can reach |
|---|---|---|---|
| `PAPER` | `POST /api/v2/auth/setup` (first run) or `POST /api/v2/auth/login` | 12 h | `/paper/*`, read-only telemetry, paper orders, paper funding |
| `LIVE` | `POST /api/v2/auth/elevate-live` with the numeric `live_pin` | 30 min | everything PAPER can, plus every live-mutating route |

The session travels as either the `X-NIOS-Session` header or the `HttpOnly; SameSite=Strict`
`nios_session` cookie. Elevation is **single-use**: the PAPER token that was elevated is
revoked the moment the LIVE token is issued, so a stolen PAPER token cannot be replayed
afterwards.

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v2/auth/status` | `GET` | `{initialized, authenticated, username, tier, clean_slate_active}` |
| `/api/v2/auth/setup` | `POST` | First-run operator registration (`username`, `password`, `live_pin`, `start_clean_slate`) |
| `/api/v2/auth/login` | `POST` | PAPER session (`username`, `password`) |
| `/api/v2/auth/elevate-live` | `POST` | Second factor (`live_pin`) → LIVE session |
| `/api/v2/auth/logout` | `POST` | Revoke the presented session |
| `/api/v2/auth/reset` | `POST` | Factory reset: delete the operator credential **and** zero the workspace. Requires `{password, live_pin}` once an operator exists — see SOP 1B.6 |

Credentials live in Ring 0 (`ring0_vault/silos/key_vault/operator_auth.py`): the password is
scrypt-hashed, the PIN is PBKDF2-derived, and sessions are HMAC-signed files under
`<vault>/operator/operator_auth.json`. Ring 3 never stores either secret.

### 1B.2 The two workspaces

| Workspace | SPA routes | State endpoint |
|---|---|---|
| Paper sandbox | `/paper`, `/paper/dashboard`, `/paper/terminal`, `/paper/orders`, `/paper/options`, `/paper/analyzer`, `/paper/quant`, `/paper/agents`, `/paper/backtest`, `/paper/brain`, `/paper/forge`, `/paper/ledger`, `/paper/logbook`, `/paper/verification`, `/paper/brokers`, `/paper/control` | `GET /api/v2/workspace/paper/state` |
| Live production | `/live`, `/live/dashboard`, `/live/terminal`, `/live/orders`, `/live/positions`, `/live/options`, `/live/analyzer`, `/live/quant`, `/live/agents`, `/live/backtest`, `/live/brain`, `/live/forge`, `/live/ledger`, `/live/logbook`, `/live/verification`, `/live/brokers`, `/live/control` | `GET /api/v2/workspace/live/state` |

The two trees are separate React entry points with separate state contracts. The Live tree
is sealed: `GET /api/v2/workspace/live/state` answers `401 AUTH_REQUIRED` with no session and
`403 LIVE_ELEVATION_REQUIRED` with a PAPER session. The legacy routes (`/dashboard`,
`/orders`, `/ledger`, `/control`, `/`) keep resolving so old bookmarks do not 404.

### 1B.3 Clean-Slate first boot

A fresh install boots with **no demo money and no running tape**:

- every silo × vault balance is `0` micros, `silo_nav_micros == 0`, and the double-entry
  invariant holds over an empty journal;
- `orders == []`, `events == ()`, `reservations == []`, `transfers == []`;
- `shadow_fills == 0`, `remaining_fills == 30`, `graduated == false`;
- `token_spend_usd == 0.0`;
- `autopilot.enabled == false` and `clean_slate_active == true`.

Setup with `start_clean_slate: true` (the default) re-asserts all of the above. Nothing is
funded until the operator funds it:

```bash
# Deposit the operator's own paper capital (double entry: CASH debit, vault credits)
curl.exe -X POST http://127.0.0.1:8000/api/v2/control/ledger/fund-paper \
  -H "X-NIOS-Session: $PAPER_TOKEN" -H "Content-Type: application/json" \
  -d '{"silo":"US_IBKR","amount_units":25000.0,"vault":"ALL_CANONICAL"}'

# Zero the workspace again without touching the credential
curl.exe -X POST http://127.0.0.1:8000/api/v2/control/workspace/reset \
  -H "X-NIOS-Session: $PAPER_TOKEN"

# Play / pause the paper tape (default OFF)
curl.exe -X POST http://127.0.0.1:8000/api/v2/control/workspace/autopilot \
  -H "X-NIOS-Session: $PAPER_TOKEN" -H "Content-Type: application/json" \
  -d '{"enabled":true}'
```

`fund-paper` rejects an unknown silo with `404 UNKNOWN_SILO` and allocates the deposit across
the canonical vaults (`ReserveYield`, `LongTermCompounder`, `TacticalSwing`, `SpinalScalper`).
Funding is the only way capital enters a workspace.

### 1B.4 The LIVE mutation guard

Once an operator credential exists on disk, four Ring 0 surfaces stop accepting PAPER-tier
sessions. The guard is inert on a first-run install (`operator_auth.initialized() == false`),
which is what keeps the legacy unit suites byte-identical.

| Route | Guard condition |
|---|---|
| `POST /api/v2/control/brokers` | only when `mode == "LIVE"` |
| `POST /api/v2/control/brokers/credentials` | always |
| `POST /api/v2/control/brokers/testnet-order` | always |
| `POST /api/v2/control/vault/rotate-keys` | always |

Failures are `401 AUTH_REQUIRED` (no/invalid session) or `403 LIVE_ELEVATION_REQUIRED`
(valid PAPER session). `POST /api/v2/control/brokers` with `mode: "PAPER"` stays open — a
Paper tab may always de-escalate a silo.

### 1B.5 Non-destructive V1.1 burn-in drill

```bash
# 1. Point the operator vault at a scratch directory so the drill cannot
#    claim the real install's credential.
$env:NIOS_OPERATOR_AUTH_DIR = "$env:TEMP\nios-v11-drill"

# 2. Boot on a free port and confirm the clean slate
python scripts/run_nios.py --host 127.0.0.1 --port 8001
#    /api/v2/workspace/paper/state -> every vault 0, orders [], autopilot false

# 3. setup -> login -> paper state -> fund-paper -> live state (403)
#    -> elevate-live -> live state (200) -> logout

# 4. Shut down cleanly; `git status --porcelain` must be empty.
```

### 1B.6 Factory reset costs both factors

`POST /api/v2/auth/reset` is the most destructive action Ring 3 can reach: it deletes
`<vault>/operator/operator_auth.json`, including the HMAC session key, and revokes every
outstanding token. A UI "confirm?" click is therefore **not** proof of identity. Once an
operator exists the call costs the same two factors as a LIVE elevation:

| Caller supplies | Ring 0 verdict | HTTP |
|---|---|---|
| no body, or one factor only | `verify_reset_credentials()` raises `ResetCredentialsMissing` | `401 AUTH_REQUIRED` |
| both factors, either one wrong | `ResetCredentialsRejected` | `403 INVALID_CREDENTIALS` |
| `password` + `live_pin` both match | vault erased, workspace zeroed | `200`, `credential.credential_verified == true` |
| nothing, on a clean slate (`initialized == false`) | not gated, no-op | `200`, `credential.removed == false` |

Verification is done in Ring 0 against the stored digests: `password` against the `scrypt`
hash, `live_pin` against the `pbkdf2_hmac` hash. Both are always derived before the comparison,
so a wrong password cannot be distinguished from a wrong PIN by response latency. The
clean-slate branch keeps first-run initialisation idempotent: resetting an uninitialised
install is a no-op, not an authentication event.

```bash
# Factory reset with the operator's own two factors (initialized install)
curl.exe -X POST http://127.0.0.1:8000/api/v2/auth/reset \
  -H "Content-Type: application/json" \
  -d '{"password":"<operator password>","live_pin":"<live pin>"}'

# Clean-slate reset (no operator on disk) — no body required
curl.exe -X POST http://127.0.0.1:8000/api/v2/auth/reset
```

The response drops the `nios_session` cookie, so the cockpit lands back on `/setup` with
`initialized: false`. The same form is on `/control` and `/paper/control` under
**Sovereign factory reset**; it prompts for `password` and `live_pin` before it calls the
route. `POST /api/v2/control/workspace/reset` remains the credential-free, non-destructive
way to zero values while keeping the operator.

---

## SOP 1C — V1.3 Separate Paper and Live Login Portals

`V1.3` gives the two workspaces their own login doors. The legacy
`/api/v2/auth/login` + `/api/v2/auth/elevate-live` pair still works and is kept for
backward compatibility, but an operator no longer has to hold a PAPER token in order to enter
the live vault.

### 1C.1 The two portals

| Portal | Route | Request | Session | Reach |
|---|---|---|---|---|
| Paper | `/paper/login` | `{username, password}` | `PAPER`, 12 h | `/paper/*`, paper orders, paper funding |
| Live | `/live/login` | `{username, password, live_pin}` | `LIVE`, 30 min | everything PAPER can reach **plus** every live-mutating route |
| Gateway | `/login` | — (selector only) | — | renders both portal cards |

| Endpoint | Method | Success | Failure |
|---|---|---|---|
| `/api/v2/auth/login/paper` | `POST` | `200`, `tier=PAPER`, `workspace=PAPER`, `session_ttl_s=43200` | `401 INVALID_CREDENTIALS` |
| `/api/v2/auth/login/live` | `POST` | `200`, `tier=LIVE`, `workspace=LIVE`, `session_ttl_s=1800`, `preflight_ok` | `403 LIVE_PIN_REJECTED` (missing/non-numeric/wrong PIN), `401 INVALID_CREDENTIALS` (bad password) |

Both routes set the `HttpOnly; SameSite=Strict` `nios_session` cookie with the matching
`max_age` (43,200 s vs 1,800 s), so the browser and the `X-NIOS-Session` header always agree.

### 1C.2 What each portal can and cannot do

* A PAPER token answers `403 LIVE_ELEVATION_REQUIRED` on
  `GET /api/v2/workspace/live/state` — by design, not by accident.
* The LIVE portal verifies the `scrypt` password digest **and** the `pbkdf2_hmac` PIN digest
  in constant time inside Ring 0; both KDFs always run, so a wrong password cannot be told
  apart from a wrong PIN by latency.
* A missing PIN is rejected before either KDF runs and is always `403 LIVE_PIN_REJECTED`
  (an authorization verdict), never a `422` schema error.
* `GET /api/v2/auth/status` now returns `login_portals`
  (`{"paper": "/paper/login", "live": "/live/login", "gateway": "/login"}`) and
  `active_workspace` (`PAPER` | `LIVE` | `NONE`), plus `expires_ns` for the live TTL chip.

```bash
# Paper portal
curl.exe -X POST http://127.0.0.1:8000/api/v2/auth/login/paper \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"sovereign\",\"password\":\"<operator password>\"}"

# Live portal: three factors in one request
curl.exe -X POST http://127.0.0.1:8000/api/v2/auth/login/live \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"sovereign\",\"password\":\"<operator password>\",\"live_pin\":\"<pin>\"}"
```

### 1C.3 The two shells look nothing alike

| | Paper sandbox | Live production |
|---|---|---|
| Palette | deep obsidian, lavender `#a78bfa` + cyan `#22d3ee` | matte carbon, crimson `#f43f5e` + amber `#f59e0b` |
| Ribbon | `PAPER SANDBOX`, `SIMULATED NAV`, autopilot play/pause, deposit, reset-to-zero, *switch to live vault* | hazard stripes, `🔒 LIVE PRODUCTION — REAL CAPITAL`, 23-gate status, live TTL countdown, `<1ms emergency kill switch`, *lock live vault (drop to paper)* |
| Affordances | deposit paper capital, reset to zero, shadow metrics | **no** deposit control, **no** shadow-burst control; the live journal is separate |

A page under `/live/*` never renders a paper deposit button or a synthetic shadow-burst
button. That absence is part of the safety contract (`sec_018` Law 2: the human, not the
model, owns the live gate).

---

## SOP 1D — V1.6 Headless Backend Mode

`v3.7.0-V1.6` ships **no frontend**. The React/Vite tree
(`ring3_cockpit/ui/openalgo_shell/src`, `public`, `node_modules`, the Vite/Biome
toolchain and the compiled JavaScript bundle) was removed. Ring 3 is a headless
REST/SSE backend plus one deterministic status document.

### 1D.1 What a browser sees

`GET /`, `/dashboard`, `/orders`, `/paper/*`, `/live/*` and every other client
route answer `200 text/html` with
`ring3_cockpit/ui/openalgo_shell/dist/index.html`: a zero-JavaScript manifest
that names the API root, the envelope keys and the risk-telemetry keys. There is
no `<script>` tag, no `/assets/` bundle and no CSS framework. `GET /api/v2/*` is
never swallowed by the catch-all; an unknown API path is a real `404`.

If `dist/index.html` is absent, `create_app()` generates the same manifest
in-process, so a bare checkout never 404s on a client route.

### 1D.2 The response envelope

Every `/api/v2/cockpit/*` and `/api/v2/workspace/*` payload carries:

| Field | Meaning |
|---|---|
| `schema_version` | `v1.6.headless-backend` |
| `generated_at_ns` | UTC nanosecond stamp of the response |
| `clean_slate_active` | workspace is still demo-free |
| `workspace_mode` | `PAPER` / `LIVE` / `NONE` |
| `payload_sha256` | SHA-256 over the canonical JSON (envelope excluded) |
| `risk` | per-silo `gross_exposure_micros`, `net_exposure_micros`, `margin_used_micros`, `free_margin_micros`, `var_99_bps`, `cvar_99_bps`, `max_drawdown_bps`, `realized_pnl_micros`, `unrealized_pnl_micros`, `total_tax_paid_micros` |

To verify a payload: remove `payload_sha256`, serialise with sorted keys and
`separators=(",", ":")`, SHA-256 the UTF-8 bytes and compare.

### 1D.3 Instrument and statutory data completeness

`core/instrument_registry.py` holds 274 canonical instruments across the four
silos with exact `lot_size`, `tick_size_micros`, `freeze_quantity`,
`circuit_limit_pct`, initial/maintenance margin, settlement cycle, UTC session
and option strike step. `SmartOMS` rejects an off-lot or over-freeze quantity
with `422 INSTRUMENT_REJECTED` and snaps an off-tick limit price onto the exact
tick grid before the outbox is touched.

`ring0_vault/ibor/tax_and_cost_engine.py` itemizes every fill's statutory drag in
integer micros and bps: India `STT / exchange / SEBI / stamp duty / GST 18% /
brokerage / modeled slippage` (`8.50 bps`), crypto `maker-taker / funding 8h /
liquidation insurance / modeled slippage` (`16.00 bps`), US `SEC 31 / FINRA TAF /
OCC / commission / modeled slippage` (`2.80 bps`) and FX `raw spread / ECN /
TOM-NEXT / modeled slippage` (`1.60 bps`). The components always sum to the
floor to the last micro-unit and are exposed on `cockpit/orders`,
`cockpit/ledger`, `cockpit/logbook`, `orders/paper` and `backtest/run`.

`ring2_brain/quant/catalog.py` exposes the 490-factor catalogue (six taxonomies,
per-factor IC / decay half-life / z-score / contribution weight), the 47 data
loaders and the 17 regional engines on `GET /api/v2/cockpit/quant`.

### 1D.4 Security hardening

* `POST /auth/login`, `/auth/login/paper`, `/auth/login/live`,
  `/auth/elevate-live` and `/auth/reset` persist a brute-force counter: five
  consecutive failures inside 15 minutes lock the door for 60 s, doubling per
  further failure to a 1 h ceiling. A locked attempt is `429
  TOO_MANY_ATTEMPTS` with `Retry-After`.
* Every request model rejects `NaN`, `±Inf` and `|x| > 1e18` at the schema
  boundary; identifier fields reject `/`, `\`, NUL and `..`.
* `IBORLedger`, `CrashAtomicOutbox`, `SmartOMS`, `WalkForwardBacktestEngine`,
  `AgentTaskDispatcher` and the control plane serialize mutations behind
  re-entrant locks. `POST /control/orders/paper` accepts `idempotency_key`: a
  duplicate replays the original receipt and never double-debits the vault.

---

## SOP 1E - V1.7 Institutional Universe, Feature Store and Catalysts

`v3.8.0-V1.7` expands the tradable catalogue from 48 to **274 canonical instruments** and
gives every one of them a deep, SHA-256-grounded feature profile. Ring 3 stays headless.

### 1E.1 What the universe now holds

| Silo | Count | Coverage |
|---|---|---|
| `INDIA_ZERODHA_DHAN` | 76 | 8 indices, all 50 NIFTY 50 constituents, 14 MCX commodity futures, 4 NSE currency futures |
| `CRYPTO_BINANCE` | 80 | 30 layer-1 / blue-chip perps + 50 DeFi, L2, AI, DePIN, RWA, gaming and meme perps |
| `US_IBKR` | 79 | 50 mega/large-cap equities, 23 benchmark and sector ETFs, 6 CME futures |
| `FX_MT5` | 39 | 28 G10 majors and crosses, 5 EM crosses, 4 precious metals, 2 energy CFDs |

Every entry carries `symbol`, `display_name`, `silo`, `venue`, `asset_class`, `sub_category`,
`sector`, `currency`, `lot_size`, `tick_size_micros`, `freeze_quantity`, `circuit_limit_pct`,
`margin_initial_pct`, `margin_maintenance_pct`, `settlement_cycle`, `trading_hours_utc`,
`option_strike_step`, `has_options` and `isin_or_id`. Symbols are unique across all four
silos, so `GET /api/v2/instruments/BTCUSDT` is never ambiguous.

### 1E.2 The five data surfaces

| Surface | Backing module | Contract |
|---|---|---|
| Contract catalogue | `core/instrument_registry.py` | Deterministic `query(silo, asset_class, sector, search, has_options, limit, offset)` |
| Attribute features | `core/market_features_store.py` | Equity fundamentals, crypto on-chain/derivatives, FX carry/COT per asset class |
| Options surface | `core/market_features_store.py` | `7DTE/30DTE/60DTE/90DTE` term structure, `iv_rank`, skew, GEX, vanna, charm, PCR, max pain |
| Sector heatmaps | `ring2_brain/quant/macro_and_catalysts.py` | India NSE, US GICS, crypto narratives with advance/decline breadth |
| Macro catalysts | `ring2_brain/quant/macro_and_catalysts.py` | Release calendar with consensus, previous, impact tier and volatility multiplier |

A paper order for a symbol that is not in the registry is now rejected with
`422 INSTRUMENT_REJECTED`, so an unknown ticker can no longer bypass the contract spec.


---

## SOP 1F - V1.9 Multi-Tranche Missions, Tranches and Environment Truth

`v3.10.0-V1.9` adds goal-driven capital missions, per-tranche risk isolation, the
dynamic profit split, SEBI/NSE compliance and effective-dated US/FX rules. Ring 3 stays
headless; every new surface below is REST-only.

### 1F.1 Mission lifecycle (three-state operator workflow)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v2/missions` | Create a mission; the Ring 2 feasibility engine may answer `422 INFEASIBLE_MISSION` |
| `GET` | `/api/v2/missions` | List every mission with its approval state, pacing and tranches |
| `GET` | `/api/v2/missions/{mission_id}` | One mission: target, deadline, risk budget, `daily_velocity_target_micros`, feasibility |
| `POST` | `/api/v2/missions/{mission_id}/approve` | Re-scope the approval (`APPROVE` or `OVERRIDE`) |
| `POST` | `/api/v2/missions/{mission_id}/revoke` | `REVOKE` the approval and halt every tranche that belongs to it |
| `POST` | `/api/v2/missions/{mission_id}/tranches` | Allocate one tranche under the mission's scoped notional cap |

A mission is only tradeable while its signed `MissionAuthorization` is live and covers the
silo and notional. `REVOKE` is immediate and halts the mission's tranches; it never touches
another mission or silo.

### 1F.2 Tranche isolation and re-arm

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v2/tranches` | List tranches (optional `?mission_id=`) with stop, HWM, heat and status |
| `GET` | `/api/v2/tranches/{tranche_id}` | One tranche's risk state |
| `POST` | `/api/v2/tranches/{tranche_id}/rearm` | Human re-arm of exactly one halted tranche |

A tranche that gives back `stop_micros` (85% of its share of the silo daily-loss limit) is
halted through `TrancheLatchTable`. `GLOBAL_HALT` is **not** tripped: sibling tranches and
unrelated silos keep trading. Re-arming resets the give-back baseline, never the realised P&L.

### 1F.3 Pacing, target lock and profit split

`MissionPacingController` throttles sizing once 80% of the target is locked
(`f_g = f_0 * max(0.25, (1-g)^2)`), caps any single UTC day at 35% of the target, and latches
`TARGET_LOCK` at 100% so a completed mission cannot give the gain back. Unrealised P&L never
raises the mission high-water mark. `DynamicFundSplitEngine` splits every realised profit in
exact integer micros across `LongTermCompounder`, `ReserveYield`, `TAX_ESCROW` and
`SpinalScalper`; the legs sum to the realised amount and `cross_border_wire_bps == 0`.

### 1F.4 Capital mobility

* **Crypto (physical):** a realised win in `CRYPTO_BINANCE` produces a signed
  `WalletSweepIntent` executed as an internal `SPOT <-> UMFUTURE_MAIN` transfer. It is gated
  by the firewall, the signer and the kill latch, and liquidity is credited only after the
  venue ack.
* **India / US / FX (virtual):** broker APIs prohibit fund transfers, so
  `VirtualMarginQuotaEngine` issues a signed, expiring, TigerBeetle-style pending reservation
  granting virtual margin headroom (`quota <= locked ReserveYield`) with a T+1
  `available_at_utc`. **No funds move** and a hot kill latch revokes the quota immediately.

### 1F.5 Environment truth and SEBI compliance

| Control | Behaviour |
|---|---|
| Exchange algo id (`B1`) | Indian orders carry `exchange_algo_id`; Ring 0 refuses a missing/`GENERIC` id when a registered one is required (`ALGO-ID-MISSING`) |
| 10 OPS bucket (`B2`) | Deterministic UTC-second integer-micro token bucket shared by Ring 1/Ring 0 (`OPS-LIMIT`) |
| Egress allowlist (`B3`) | Only registered static IPs may originate Indian orders (`EGRESS_IP_NOT_WHITELISTED`) |
| TOTP renewal (`B4`) | 03:00 UTC (08:30 IST) pre-market renewal with TTL telemetry |
| Freeze slicing (`B5`) | F&O orders above `freeze_quantity` become compliant child slices under one parent key |
| Hedge exit (`B6`) | High-margin/short leg closes first, protective hedge last (`HEDGE_EXIT_ORDERING_VIOLATION`) |
| Effective-dated rules (`D1`) | FINRA 4210 intraday margin replaces the `$25k` PDT floor on `2026-06-04`; historical dates keep the legacy row |
| Locate fee / borrow (`D2`) | Short theses carry `locate_fee_bps` + `borrow_confirmed`; an unconfirmed borrow is refused |
| SSR (`D4`) / extended hours (`D5`) | Uptick-only shorts while SSR is active; a spread cap applies outside regular hours |

### 1F.6 Clock, calendar, staleness and replay

`GET /api/v2/cockpit/clock` reports the real monotonic-anchored clock offset
(`synthetic: false`); a breach fails closed at `FirewallStage.TIME_HEALTH`. `core/exchange_calendar.py`
is DST-aware for all four silos and refuses an order outside a session (`VENUE_CLOSED`).
`LocalJetStreamBus.published_high_water` drives `STALE_POSITION_STATE` when a read model trails
the published watermark beyond `staleness_budget`. `AtomicStateJournal.write_checkpoint`
binds a SHA-256 state digest to an event boundary, so a replay can prove it reached the right
event.

### 1F.7 Release verification for `v3.10.0-V1.9`

```bash
python -m pytest tests -q                 # 31 suites, 711 passed, 0 failed, 0 warnings
python scripts/run_nios.py --smoke-test   # ROWS=23, EXIT=0
python scripts/preflight_golive.py --fast # 23/23 PASS, EXIT=0
```

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
python -m pytest tests -q                        # 31 suites, 0 failures, 0 warnings
python scripts/preflight_golive.py --strict      # go-live readiness certificate, 23/23
python scripts/preflight_golive.py --fast        # replay smoke receipt (interactive), 23/23
```

Release seal for `v3.10.0-V1.9`: `pytest tests -q` must report **100% pass across all 31 suites, 0 failed, 0 warnings**
and `python scripts/preflight_golive.py --fast` must report **23/23 PASS, EXIT=0**. Both are
prerequisites for re-tagging; neither may be waived.

### 3.2a The 31-suite inventory

| # | Suite | Covers |
|---|---|---|
| 1 | `tests/test_step2_core_spine.py` | SHM SeqLock, NATS contracts, bitemporal lake |
| 2 | `tests/test_step3_truth_and_quant.py` | SHA-256 grounding ledger, 480+ factors, DCF/Comps |
| 3 | `tests/test_step4_ring2_cognition.py` | System 1 lease writer, TradingAgents committee |
| 4 | `tests/test_step5_ring1_spinal.py` | 0.14 ms scalper, breakers, feed quarantine |
| 5 | `tests/test_step6_ring0_vault.py` | Double-entry IBOR, outbox, conformal gate |
| 6 | `tests/test_step7_cockpit_and_e2e.py` | Cockpit tabs, hot-swap, kill switch |
| 7 | `tests/test_step8_forge_mcp_and_deploy.py` | Forge, read-only MCP bridge, deploy |
| 8 | `tests/test_step9_paper_autopilot.py` | Paper autopilot tape |
| 9 | `tests/test_phase2_step1_donor_activation.py` | Donor activation, ring isolation |
| 10 | `tests/test_phase2_step2_silo_adapters.py` | Four silos, PAPER→LIVE toggle |
| 11 | `tests/test_phase2_step3_feeds_and_llm_pool.py` | Dual-vendor feed, LLM pool budgets |
| 12 | `tests/test_phase2_step4_formal_vopr_and_hedger.py` | TLA+/VOPR, tail hedger |
| 13 | `tests/test_phase3_step1_multiprocess_and_live_daemons.py` | Four-ring OS processes |
| 14 | `tests/test_phase3_step2_live_cutover_and_cockpit.py` | Live cutover, masked credentials |
| 15 | `tests/test_phase3_step3_testnet_supervisor_and_security.py` | Testnet bridge, supervisor, security |
| 16 | `tests/test_phase3_step4_soak_metrics_and_chaos.py` | Soak metrics, chaos drills |
| 17 | `tests/test_phase3_step5_os_tuning_key_rotation_and_runbook.py` | OS tuning, key rotation, runbook |
| 18 | `tests/test_phase4_final_preflight_and_supervisor_forge.py` | 23-gate pre-flight, forge |
| 19 | `tests/test_v1_1_auth_dual_workspace_and_clean_slate.py` | **V1.1** operator auth, dual workspaces, clean slate, LIVE mutation guard, credential-gated factory reset |
| 20 | `tests/test_v1_2_agent_tasks_backtesting_and_daily_analysis.py` | **V1.2** `daily_stock_analysis` harvest (8 strategies, grounded decision dashboard), 6 specialist agent tasks, tax-net walk-forward backtester, 30-fill nursery gate, 11-page SPA |
| 21 | `tests/test_v1_3_redesigned_dual_ui_separate_logins_and_15_pages.py` | **V1.3** separate `/paper/login` + `/live/login` portals, options chain with Greeks/PCR/Max Pain, six multi-leg payoff templates, market terminal, cost-adjusted logbook, 12-venue broker hub, 15 pages across all three route trees |
| 22 | `tests/test_v1_4_clean_room_masterpiece_ui.py` | **V1.4** clean-room rebuild: 15 bespoke workstation pages, dual-shell separation, four auth portals, six zero-dependency SVG charts, biome-clean source, clean-slate rendering |
| 23 | `tests/test_v1_5_institutional_terminal_ui.py` | **V1.6** headless institutional contract: 50-route status document, integer micro money, exact itemized statutory floors, 15-page catalogue, dual workspace boundary, reproducible payload digest |
| 24 | `tests/test_v1_6_backend_bug_hunt_security_and_data_completeness.py` | **V1.6** master instrument registry + lot/tick/freeze enforcement, itemized statutory cost engine, adversarial numerical/boundary fuzzing, 32-thread idempotency + double-entry conservation, auth brute-force lockout + secret non-leakage |
| 25 | `tests/test_v1_7_deep_market_universe_and_feature_audit.py` | **V1.7** 274-instrument universe completeness, deep asset-class feature store + options surface with SHA-256 grounding, instrument search/filter/pagination, macro calendar + four cross-asset heatmaps, SmartOMS + walk-forward lot/tick/statutory-floor enforcement |
| 26 | `tests/test_v1_8_tradingview_bridge_streaming_and_handshake.py` | **V1.8** `Mathieu2301/TradingView-API` harvest into Ring 2, 274 canonical `EXCHANGE:SYMBOL` tickers, deterministic replay chart + eight Pine Script study evaluators under SHA-256 grounding, `TRADINGVIEW_REPLAY` backtests on statutory floors, multiplexed typed SSE stream hub, 12-venue broker handshake |
| 27 | `tests/test_v1_8_deep_gap_closure_and_cross_engine_audit.py` | **V1.8** universal options chain + terminal quote across all 274 registry instruments (registry `option_strike_step`, `OptionsSurfaceProfile` GEX/`iv_rank`/max-pain, tick-snapped L2), `INSTRUMENT_NOT_OPTIONABLE` on spot FX, segment- and side-aware Finance Act 2026 statutory tax, circuit-band / margin / drawdown-breaker enforcement, crash-safe atomic state journal with Clean-Slate reset, SSE disconnect + `1..500` bound fuzzing + 16-reader concurrency |
| 28 | `tests/test_v1_9_mission_tranche_and_profit_split.py` | **V1.9** mission contract + three-state authorization, half-Kelly feasibility (`422 INFEASIBLE_MISSION`), per-tranche stop isolation without `GLOBAL_HALT`, correlation independence gate (`N_eff`, 2% buckets, 6% heat), goal pacing / best-day cap / `TARGET_LOCK`, dynamic integer-micro profit split with monotonic reserve floor, physical crypto wallet sweeps, virtual India/US/FX margin quotas, `/api/v2/missions` + `/api/v2/tranches` |
| 29 | `tests/test_v1_9_sebi_and_regulatory_versioning.py` | **V1.9** SEBI/NSE algo-id tagging (`ALGO-ID-MISSING`), global 10 OPS bucket + OTR penalty accrual, static egress IP allowlist, 03:00 UTC pre-market TOTP renewal, freeze-quantity auto-slicing, hedge-leg exit ordering, effective-dated FINRA 4210 vs legacy PDT rules, locate fee / borrow confirmation, SSR uptick and extended-hours spread guards |
| 30 | `tests/test_v1_9_oms_fsm_binance_and_env_truth.py` | **V1.9** published high-water staleness (`STALE_POSITION_STATE`), all-or-none `LegGroup` baskets, cold-start reconciliation classes + bounded deadline, strict order FSM with `ExecID` de-duplication and venue `LeavesQty`, real clock health, DST-aware exchange calendar, fencing epoch, SHA-256 replay checkpoints, Binance 418 halt / query-before-retry / weight bucket / margin brackets / listenKey / recvWindow, ADL budget and funding in the net bound |
| 31 | `tests/test_v1_9_ai_provenance_and_concept_drift.py` | **V1.9** evidence source allowlist + retrieval attestation + freshness lower bound, prompt-injection guard, hierarchical star aggregation, frozen signed-order inputs re-verified before signing, ADWIN/conformal-martingale concept drift (halve then pause), agent behaviour monitor, Gate 23 registration and 23/23 certificate |


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
in the silo is `ready`, the kill latch is clear, **and the 23-gate pre-flight certificate
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

### 4.4 23-gate go-live certificate

```bash
curl.exe -s http://127.0.0.1:8000/api/v2/control/preflight            # fast path
curl.exe -s -X POST http://127.0.0.1:8000/api/v2/control/preflight \
  -H "Content-Type: application/json" -d "{\"full_smoke\":true}"      # full re-run
python scripts/preflight_golive.py --strict                           # CLI, release gate
```

The fast path replays a short-lived in-process certificate and the persisted receipt of the
last certified 23-row smoke suite for the current commit, so the cockpit stays responsive.
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

#### 5.3.2 Page telemetry — 9 read-only endpoints

One endpoint per non-dashboard page. Each returns the page header, ring ownership, live organ
counts and the row set the SPA renders. None of them mutates state, opens a socket, or reads a
credential.

| Method | Path | Backing surface |
|---|---|---|
| `GET` | `/api/v2/cockpit/pages` | 15-page catalogue, alias map, platform-wide counts |
| `GET` | `/api/v2/cockpit/terminal` | Market terminal watchlist + a grounded 60-bar / L2 / tape frame |
| `GET` | `/api/v2/cockpit/orders` | SmartOMS blotter, Maker-Peg/TWAP slicers, outbox |
| `GET` | `/api/v2/cockpit/options` | Optionable underlyings, six payoff templates, live NIFTY chain |
| `GET` | `/api/v2/cockpit/analyzer` | 30-fill L2 shadow nursery, adverse selection |
| `GET` | `/api/v2/cockpit/quant` | 490-factor scan, DCF / Black-Scholes, loaders |
| `GET` | `/api/v2/cockpit/forge` | Alpha Forge leaderboard, plugins, SKILL.md catalog |
| `GET` | `/api/v2/cockpit/ledger` | Double-entry journal, 35/15 ratchet, TigerBeetle + PG16 |
| `GET` | `/api/v2/cockpit/logbook` | Trade journal, cost-drag aggregates, silo cost floors |
| `GET` | `/api/v2/cockpit/agents` | 6 specialist agents, 8 strategies, task queue, token ledger |
| `GET` | `/api/v2/cockpit/backtest` | 8-strategy catalog, 5 timeframes, silo cost floors, saved runs |
| `GET` | `/api/v2/cockpit/brokers` | 12-venue adapter matrix, credential status, Ed25519 dry-run, feed health |
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
| `GET`/`POST` | `/api/v2/control/preflight` | 23-gate go-live readiness certificate |
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

#### 5.3.5 V1.1 operator auth, V1.3 dual portals and clean slate

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v2/auth/status` | `initialized`, `authenticated`, `username`, `tier`, `workspace`, `clean_slate_active`, `login_portals`, `active_workspace`, `expires_ns` |
| `POST` | `/api/v2/auth/setup` | First-run operator registration → PAPER session |
| `POST` | `/api/v2/auth/login` | Legacy PAPER session |
| `POST` | `/api/v2/auth/login/paper` | **V1.3** dedicated paper portal → PAPER (12 h) |
| `POST` | `/api/v2/auth/login/live` | **V1.3** dedicated live portal, three factors → LIVE (30 min) |
| `POST` | `/api/v2/auth/elevate-live` | Legacy PAPER→LIVE PIN elevation (30 min, single-use) |
| `POST` | `/api/v2/auth/logout` | Revoke the presented session |
| `POST` | `/api/v2/auth/reset` | Delete the operator credential and zero the workspace (`{password, live_pin}` required once initialized) |
| `GET` | `/api/v2/workspace/paper/state` | Mode-scoped Paper telemetry |
| `GET` | `/api/v2/workspace/live/state` | Mode-scoped Live telemetry (`tier=LIVE` only) |
| `POST` | `/api/v2/control/ledger/fund-paper` | Operator-chosen paper capital, double entry |
| `POST` | `/api/v2/control/workspace/reset` | Zero the workspace (including the logbook), keep the credential |
| `POST` | `/api/v2/control/workspace/autopilot` | Play / pause the paper tape (default OFF) |

`/api/v2/control/brokers` (`mode=LIVE`), `/brokers/credentials`, `/brokers/testnet-order` and
`/vault/rotate-keys` require a `LIVE` session once an operator exists — see SOP 1B.4.

#### 5.3.6 MCP bridge and metrics

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v2/mcp/tools` | Read-only MCP catalogue + air-gap audit |
| `POST` | `/api/v2/mcp/call` | Invoke one read-only MCP tool |
| `GET` | `/api/v2/metrics` | Prometheus exposition |
| `GET` | `/api/v2/metrics/json` | JSON telemetry |
| `GET` | `/metrics` | Prometheus scrape alias |

#### 5.3.7 V1.2 agent task hub and timeframe backtester

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v2/control/agents/assign` | Dispatch one grounded specialist-agent task |
| `POST` | `/api/v2/control/agents/batch-watchlist` | One decision-dashboard batch over a watchlist; ranks by `signal_score` + `operation_advice`; seals the batch ranking into one SHA-256 packet |
| `POST` | `/api/v2/control/agents/followup` | Grounded multi-turn strategy Q&A against a completed task; appends to `conversation_history`; re-verified by `NumericalGroundingVerifier` |
| `GET` | `/api/v2/control/agents/tasks/{task_id}` | Full grounded report plus the Q&A conversation |
| `POST` | `/api/v2/control/agents/cancel` | Cancel one task or clear the queue |
| `POST` | `/api/v2/control/backtest/run` | Tax-net walk-forward run; `timeframe` is `1m`/`5m`/`15m`/`1h`/`1d` |
| `POST` | `/api/v2/control/backtest/compare-strategies` | All 8 harvested strategies on one shared bar series, ranked, with the silo tax-net floor deducted |
| `POST` | `/api/v2/control/backtest/promote-to-nursery` | DSR-gated entry into the 30-fill L2 shadow nursery |

Cost floors applied by both backtest routes: `INDIA_ZERODHA_DHAN` 8.50 bps (includes 5.50 bps
STT), `CRYPTO_BINANCE` 16.00 bps, `US_IBKR` 2.80 bps, `FX_MT5` 1.60 bps. An unknown
`timeframe`, silo or strategy is rejected with `422`; an unknown task is `404 UNKNOWN_TASK`.

#### 5.3.7 V1.3 options, terminal, logbook and broker hub

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v2/cockpit/options` | Underlyings, six payoff templates and a grounded NIFTY chain |
| `POST` | `/api/v2/control/options/chain` | Deterministic Call/Put chain with Greeks, PCR, Max Pain, ATM IV (11 strikes by default) |
| `POST` | `/api/v2/control/options/strategy-payoff` | Multi-leg payoff: legs, net debit/credit, max profit/loss, breakevens, 25-point curve |
| `GET` | `/api/v2/cockpit/terminal` | Watchlist + a live 60-bar / 10-level / Lee-Ready frame |
| `POST` | `/api/v2/control/terminal/quote` | Re-frame one symbol (quotes, OHLCV, MA5/10/20, VWAP, L2 ladder, tape) |
| `GET` | `/api/v2/cockpit/logbook` | Journal entries, gross/net P&L, win rate, avg R, cost floors |
| `POST` | `/api/v2/control/logbook/entry` | Record one cost-adjusted trade (gross → cost drag → net → R-multiple) |
| `POST` | `/api/v2/control/logbook/clear` | Zero the journal |
| `GET` | `/api/v2/cockpit/brokers` | 12-venue matrix, `CONFIGURED`/`UNSET`, masked keys, Ed25519 dry-run, settlement |
| `POST` | `/api/v2/control/brokers/ping` | Import one adapter and prove the Ring 0 Ed25519 signer round-trips |

The chain supports `NIFTY`, `BANKNIFTY`, `BTCUSDT`, `ETHUSDT`, `SPY` and `EURUSD`, with an
optional `spot`, `volatility`, `days_to_expiry`, `risk_free_rate` and `strike_count`
(default `11`, forced odd so the chain stays symmetric). Every response is sealed with a
verified SHA-256 `GroundedFactPacket`.

The logbook applies the statutory cost floor per silo before attribution: `INDIA_ZERODHA_DHAN`
8.50 bps, `CRYPTO_BINANCE` 16.00 bps, `US_IBKR` 2.80 bps, `FX_MT5` 1.60 bps. A clean-slate
boot starts with `entries == []`, and `POST /api/v2/control/workspace/reset` clears it back to
`[]` along with every other workspace value.

#### 5.3.8 V1.7 instrument discovery, deep features, heatmaps and macro

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v2/instruments` | Paginated contract catalogue: `silo`, `asset_class`, `sector`, `search`, `has_options`, `limit` (1..500), `offset` (>= 0) |
| `GET` | `/api/v2/instruments/{symbol}` | Contract spec + asset-class feature profile + options surface + itemized statutory cost + grounded decision score |
| `GET` | `/api/v2/market/heatmaps` | India sectors, US GICS sectors, crypto narratives and the G10 currency strength matrix |
| `GET` | `/api/v2/market/macro-calendar` | Release schedule with optional `?region=` and `?impact=` (HIGH / MEDIUM) filters |

Each response carries `schema_version`, `generated_at_ns`, `payload_sha256` and a per-block
`grounding` object whose `payload_sha256` is a verified `GroundedFactPacket` digest. An
unknown silo, asset class, sector, region, impact tier, over-long search string or
out-of-range page is `422`; an unknown symbol is `404 INSTRUMENT_NOT_FOUND`.

---

## SOP 6 — Fifteen-Page Cockpit Workstation

### 6.1 The fifteen pages

The cockpit is a single React 19 SPA with fifteen production pages. `GET /api/v2/cockpit/pages`
returns the authoritative catalogue, including the alias map and the five sidebar groups; a
direct navigation or browser refresh on any route is served by the SPA catch-all and never
404s.

| # | Page | Route | Aliases | Ring | Group | Summary |
|---|---|---|---|---|---|---|
| 1 | Dashboard | `/dashboard` | `/` | Ring 3 | Core Execution | 4-silo IBOR vault cards, SSE proof stream, 60 s leases |
| 2 | Market Terminal | `/terminal` | — | Ring 1 | Core Execution | Multi-silo watchlist, 60 bars + MA/VWAP, 10-level L2, Lee-Ready tape |
| 3 | Orders & Positions | `/orders` | `/positions` | Ring 0 | Core Execution | SmartOMS blotter, Maker-Peg/TWAP slicers, atomic outbox |
| 4 | Options & Strategy Lab | `/options` | `/chain` | Ring 2 | Quant & Options | Call/Put chain with Greeks, PCR, Max Pain, six payoff templates |
| 5 | Shadow Analyzer | `/analyzer` | — | Ring 1 | Quant & Options | 30-fill L2 shadow nursery, adverse-selection telemetry |
| 6 | Quant & Valuation Lab | `/quant` | — | Ring 2 | Quant & Options | 490-factor scanner, DCF/Black-Scholes, loader readiness |
| 7 | Agent Task Hub | `/agents` | — | Ring 2 | Bicameral Brain & Agents | 6 specialist agents, watchlist batch ranking, grounded Q&A, token ledger |
| 8 | Walk-Forward Backtester | `/backtest` | — | Ring 2 | Bicameral Brain & Agents | 5 timeframes, 8-strategy tax-net comparison, Purged K-Fold, DSR gate |
| 9 | Brain & Contagion X-Ray | `/brain` | `/xray` | Ring 2 | Bicameral Brain & Agents | Ring isolation, MiroFish cascade, committee, SHA-256 proofs |
| 10 | Alpha Forge & Plugins | `/forge` | — | Ring 2 | Bicameral Brain & Agents | Nocturnal Forge leaderboard, plugins, SKILL.md catalog |
| 11 | IBOR Ledger & Tax | `/ledger` | — | Ring 0 | Ledger & Verification | Double-entry journal, 35/15 ratchet, TigerBeetle + PG16 |
| 12 | Trade Logbook | `/logbook` | `/journal` | Ring 0 | Ledger & Verification | Cost-adjusted journal, net P&L, R-multiple, win/loss attribution |
| 13 | Proofs, Chaos & Telemetry | `/verification` | — | Ring 0 | Ledger & Verification | TLA+ BFS proofs, VOPR campaign, OS tuning, metrics |
| 14 | Broker Hub & Feeds | `/brokers` | `/venues` | Ring 0 | Sovereign Control | 12-venue matrix, masked credentials, Ed25519 dry-run, feed health |
| 15 | Control Room & MCP | `/control` | `/mcp` | Ring 3 | Sovereign Control | 23-gate pre-flight, credential vault, LLM switchboard, MCP |

`/api` and `/metrics` are excluded from the catch-all, so an unknown API path still returns a
real `404` instead of the SPA shell.

### 6.2 Which page writes what

| Page | Read path | Write path |
|---|---|---|
| `/dashboard` | `state`, `stream` | — |
| `/terminal` | `cockpit/terminal` | `terminal/quote` |
| `/orders` | `cockpit/orders` | `orders/paper`, `orders/cancel` |
| `/options` | `cockpit/options` | `options/chain`, `options/strategy-payoff` |
| `/analyzer` | `cockpit/analyzer` | `analyzer/shadow-burst` |
| `/quant` | `cockpit/quant` | `quant/price-option`, `quant/scan` |
| `/agents` | `cockpit/agents` | `agents/assign`, `agents/batch-watchlist`, `agents/followup`, `agents/cancel` |
| `/backtest` | `cockpit/backtest` | `backtest/run`, `backtest/compare-strategies`, `backtest/promote-to-nursery` |
| `/brain` | `state`, `formal-proofs` | `brain/mirofish-shock`, `brain/committee-debate` |
| `/forge` | `cockpit/forge`, `skills/{name}` | `forge/evaluate-challenger` |
| `/ledger` | `cockpit/ledger` | `ledger/reconcile` |
| `/logbook` | `cockpit/logbook` | `logbook/entry`, `logbook/clear` |
| `/verification` | `formal-proofs` | `verification/chaos-drill`, `/vopr`, `/os-tune` |
| `/brokers` | `cockpit/brokers`, `control/preflight` | `brokers/ping` |
| `/control` | `cockpit/mcp`, `control/preflight` | `brokers`, `llms`, `killswitch`, `vault/*` |

### 6.3 Non-destructive 15-page burn-in sweep

Run this before declaring a documentation or UI release sealed. It never places a live order
and never mutates a credential.

```bash
# 1. Pick a free port (never fight a daemon for 8000)
python scripts/nios_port_check.py --scan 8000 8010

# 2. Boot the unified server on the port it printed
python scripts/run_nios.py --host 127.0.0.1 --port <PORT>

# 2b. Mint a temporary operator session (V1.8 auth closure).
#     Every /api/v2/control/* mutation is authenticated as soon as
#     operator_auth.is_initialized() is true, so the sweep either runs on a
#     scratch vault or logs in with the real operator's own two factors:
#
#       NIOS_OPERATOR_AUTH_DIR=$(mktemp -d) python scripts/run_nios.py ...
#
#     curl -s -X POST localhost:<PORT>/api/v2/auth/setup \
#       -H 'content-type: application/json' \
#       -d '{"username":"burnin.operator","password":"Burn-In-Operator-2026",
#            "live_pin":"8642","start_clean_slate":false}'      # -> token
#     curl -s -X POST localhost:<PORT>/api/v2/auth/elevate-live \
#       -H "X-NIOS-Session: $TOKEN" \
#       -d '{"live_pin":"8642"}'                               # -> LIVE token
#
#     Every control call below then carries -H "X-NIOS-Session: $LIVE".

# 3. Sweep all 15 SPA routes x 3 trees (base, /paper/*, /live/*) = 45 routes
#    /dashboard /terminal /orders /options /analyzer /quant /agents /backtest
#    /brain /forge /ledger /logbook /verification /brokers /control
#    / /positions /xray /mcp /chain /journal /venues
#    /login /paper/login /live/login /setup /live-unlock

# 4. Sweep the page-telemetry endpoints + skills document
#    /api/v2/cockpit/pages /terminal /orders /options /analyzer /quant /forge
#    /ledger /logbook /agents /backtest /brokers /mcp
#    /api/v2/cockpit/skills/<skill_name>

# 5. Sweep the workstation controls with their documented payloads (expect 200)
#    plus the V1.3 controls: options/chain, options/strategy-payoff,
#    terminal/quote, logbook/entry, logbook/clear, brokers/ping
#
#    Auth tiers (V1.8), all inert until an operator credential exists:
#      PAPER or LIVE session -- orders/*, options/*, terminal/*, agents/*,
#        backtest/*, logbook/*, ledger/fund-paper, ledger/reconcile,
#        analyzer/shadow-burst, quant/*, brain/*, forge/*, verification/*,
#        extensions, feeds, llms, workspace/autopilot, killswitch
#      LIVE elevation only  -- rearm, supervisor (POST), supervisor/forge-hedge,
#        brokers, brokers/ping, brokers/handshake, brokers/credentials,
#        brokers/testnet-order, vault/rotate-keys, workspace/reset for
#        workspace=live|all (which also costs confirm=true)

# 6. Shut down cleanly; `git status --porcelain` must be empty afterwards.
```

Acceptance: **45/45 routes**, **13/13 page endpoints**, **12/12 controls**, **5/5 V1.2
controls** and **6/6 V1.3 controls** at `200 OK` **with a LIVE session header**, then a clean
`Ctrl+C` and an empty `git status --porcelain`. Any non-200 is a release blocker — do not retry
around it, read the response body.

Negative controls that must be asserted in the same sweep (V1.8):

| Request | Expected |
|---|---|
| `POST /control/orders/paper` with no session, initialized install | `401 AUTH_REQUIRED` |
| `POST /control/killswitch` with a session but no body | `400 confirm=true` required |
| `POST /control/killswitch` with `{"confirm": false}` | `400` |
| `POST /control/rearm` with a PAPER session | `403 LIVE_ELEVATION_REQUIRED` |
| `POST /control/workspace/reset {"workspace":"all"}` with a PAPER session | `403 LIVE_ELEVATION_REQUIRED` |
| `POST /control/workspace/reset {"workspace":"all","confirm":true}` with PAPER | `403 LIVE_ELEVATION_REQUIRED` |
| `POST /control/supervisor {"action":"status"}` with no session | `401 AUTH_REQUIRED` |

### 6.4 Reference payloads

Every payload below needs `-H "X-NIOS-Session: $LIVE"` (or `$TOKEN` for the paper-only
routes) once the install has an operator credential.

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

POST /api/v2/control/agents/batch-watchlist
{"silo":"US_IBKR","symbols":["SPY","QQQ","IWM","DIA"],"strategy_id":"ma_golden_cross"}
POST /api/v2/control/agents/followup
{"task_id":"task_<id>","question":"What are the target and stop levels, and the main risk?"}
POST /api/v2/control/backtest/run
{"silo":"US_IBKR","symbol":"SPY","strategy_id":"ma_golden_cross","bars":252,"timeframe":"5m"}
POST /api/v2/control/backtest/compare-strategies
{"silo":"INDIA_ZERODHA_DHAN","symbol":"RELIANCE","timeframe":"1d","bars":252}

# V1.8 destructive surfaces: the flags are real, not decorative.
POST /api/v2/control/killswitch        {"reason":"manual halt","flatten":true,"confirm":true}
POST /api/v2/control/rearm             {"reason":"operator re-arm"}
POST /api/v2/control/workspace/reset   {"workspace":"paper"}
POST /api/v2/control/workspace/reset   {"workspace":"all","confirm":true}   # LIVE only
```

`committee-debate` (and `orders/paper`) reject an unknown `silo` with `422`; use a real silo id
(`CRYPTO_BINANCE`, `INDIA_ZERODHA_DHAN`, `US_IBKR`, `FX_MT5`), not the short model default.

A rejected challenger, a chaotic drill that reports a violation, or a committee verdict of
`NO_TRADE` is **data, not an error**: fix the cause or accept the abstention. Never widen a
threshold or bypass Ring 0 to turn a red row green.

`evaluate-challenger` now also refuses `PROMOTED` unless the *recorded* shadow fills carry a
positive mean net-of-cost expectancy (`mean_net_bps > 0`). A high `candidate_sharpe` alone does
not graduate a challenger.

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
