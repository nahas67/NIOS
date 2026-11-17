# AGENTS_V2_1_BACKEND.md — V2.1 Donor-Harvest & Closed-Loop Integration Rules

**Document ID:** NIOS-ARC-2026-V3-V2.1-BACKEND
**Version:** 3.11.0-V2.1
**Branch:** `V2.1` (branched from the sealed headless backend `V1.9` @ `7287899`)
**Status:** Closed-loop and live-readiness backend (`G1`–`G12`)

This document adds four **binding** rules for the `V2.1` donor-harvest and closed-loop
integration pass. It does **not** replace `AGENTS.md`; it narrows it. Everything in
`AGENTS.md` and `NIOS_Core_architecture.json` still applies, and where the two disagree
the architecture JSON wins.

---

## Rule 1 — Clean Extraction Only

Reference donor repositories live **only** outside the NIOS repository, at
`C:\Users\nahas\OneDrive\Desktop\clone\<repo_name>` (shallow `--depth 1` clones).

To use donor material:

1. **Read** the donor file(s) in the external clone directory.
2. **Translate** the state-machine pattern, arithmetic formula, or normalized schema into a
   clean, typed, self-contained NIOS Python module inside one of:
   - `core/`
   - `ring0_vault/` (including `ibor/`, `kernel/`, `silos/`)
   - `ring1_spinal/`
   - `ring2_brain/`
   - `ring3_cockpit/gateway/`
3. **Never vendor** a raw donor repository, a `node_modules/` tree, a `.github/` tree, or a
   nested `.git/` directory into the NIOS working tree. `NESTED_GIT=0` is a release gate.

Forbidden imports: NIOS runtime code must never `import` from the external clone
directory, from `_donor_staging/`, or from any path outside the repository. Harvested
behaviour is re-implemented, not referenced.

Minimal runtime dependencies stay restricted to lightweight, pure-Python packages
(`apscheduler`, `sse-starlette`, `pydantic-settings`, `tenacity`, lazy `ccxt`). Heavy
frameworks (`nautilus_trader`, `freqtrade`, `openalgo`, `graphiti`, `mem0`, `ib_async`,
`alpaca-py`, `temporalio`) are **pattern donors only** and must never be added to
`nios-venv` as runtime dependencies.

---

## Rule 2 — Float-to-Micros Firewall

Every float price, quantity, fee, funding rate, balance, or P&L value returned by an
external SDK (`ccxt`, `ib_async`, `alpaca-py`, broker REST payloads, signal files) must be
quantized at the **Ring 0 ingress boundary** before it may enter any of:

- `IBORLedger` (accounts, transfers, vaults)
- `CrashAtomicOutbox` (reservations, commits)
- `DynamicFundSplitEngine` / `ProfitSplitSagaCoordinator`
- `VirtualMarginQuotaEngine`
- `CryptoWalletSweeper`

The only legal conversions are:

```python
from ring0_vault.ibor.ledger_outbox import to_micros
micros = to_micros(float_value)              # half-up, exact for |value| < 2**53
micros = Decimal(str(value)).scaleb(6) ...   # explicit Decimal quantization
```

Rules that follow from this:

- **No float money** ever reaches a ledger call. A `float` argument to a money path is a
  defect, not a convenience.
- A venue payload that cannot be quantized (NaN, `inf`, `None`, overflowing magnitude)
  must raise a typed rejection (`ConnectorError(code="PRICE_UNQUANTIZABLE", ...)`); it must
  never be coerced to `0`.
- `ConnectorRuntime` is the single ingress surface for venue numerics. Every adapter
  normalizes to integer micros **before** returning, so callers above it never see floats.

---

## Rule 3 — Deterministic Offline Default

Live venue adapters, the background lifecycle scheduler, and the market-data aggregator
must all default to deterministic offline behaviour:

- `ConnectorRuntime` defaults to `connector_mode="OFFLINE_REPLAY"`.
- `allow_network=False` by default on every adapter and on `LedgerStore(probe=False)`.
- `LiveBarAggregator`, `NiosLifecycleWorker`, and `MultiTrancheAutopilot` must expose a
  synchronous, clock-injected tick (`run_maintenance_tick(now_utc=...)`,
  `ingest(..., now_ns=...)`, `run_cycle(now_ns=...)`) so tests and air-gapped deployments
  execute with zero sockets and zero wall-clock dependence.
- `SANDBOX_TESTNET` and `LIVE_VENUE` are reachable **only** through an explicit operator
  runtime setting (`POST /api/v2/settings`), never through an import-time default, an
  environment guess, or a retry.

`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` stays mandatory for every test invocation so the
suite result is independent of whatever is installed in the interpreter.

---

## Rule 4 — Air-Gap Preservation

Ring 2 modules — `EntityMemoryGraph`, `PerformanceAnalytics`, `LiveBarAggregator`,
`MultiTrancheAutopilot`, and every `ring2_brain/quant/*` compute unit — must never:

- import `ring0_vault` signing, key-vault, or credential modules;
- open a broker socket, WebSocket, or credential-bearing HTTP session;
- construct an order signature.

Ring 2 emits **state and intent descriptions** only. Ring 0/3 injects the state it needs
and executes the signed intent. Concretely:

- `MultiTrancheAutopilot` returns a list of tranche-tagged dispatch proposals. `SmartOMS`
  (Ring 0) validates and reserves them; Ring 3 triggers the cycle.
- `PerformanceAnalytics` reads ledger snapshots handed to it as plain mappings/rows.
- `LiveBarAggregator` consumes ticks from a feed that Ring 1 owns.
- `PostMortemRecorder` writes to `EntityMemoryGraph`; the durable copy rides the Ring 0
  `AtomicStateJournal`, written by the caller that owns the journal.

---

## Cross-cutting invariants (unchanged, restated for this pass)

1. Money is integer micro-units end to end; `1 unit = 1_000_000 micros`.
2. Every numeric claim carries a SHA-256 commitment.
3. No LLM call, external HTTP call, or unbudgeted DNS lookup inside the signing path.
4. `connector_mode` and every live-risk knob are journal-backed runtime settings, not
   module constants.
5. Paper mode is the default; live mode requires an explicit operator action with a
   LIVE-elevated session.
6. UTC everywhere.
7. A partial failure in a multi-step capital movement must be recoverable from the
   `AtomicStateJournal` alone — no in-memory-only compensation state.
