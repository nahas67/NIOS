"""Print the NIOS Section 4.3 Latency Scorecard from live measurements.

Every row is measured in this process (or read from the signed 22-gate
certificate) on the commit at HEAD.  Status glyphs: PASS / WARN / FAIL.
"""

from __future__ import annotations

import json
import subprocess
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY = sys.executable
ROWS: list[tuple[str, str, str, str, str]] = []  # capability, budget, measured, status, evidence


def add(capability: str, budget: str, measured: str, ok: bool, evidence: str) -> None:
    ROWS.append((capability, budget, measured, "PASS" if ok else "FAIL", evidence))


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


# ---------------------------------------------------------------------------
# 1. Ring 1 tick-to-intent (1000 ticks, pinned core)
# ---------------------------------------------------------------------------
from core.schemas import LEASE_TTL_NS, ExecutedFlowTick, LeaseMode, System1Lease
from core.shm_ipc import (
    GlobalKillLatch,
    LeaseTableSHM,
    ShadowBookSHM,
    unlink_all,
)
from ring1_spinal.shadow_twin import REQUIRED_SHADOW_FILLS, ShadowExecutionTwin
from ring1_spinal.silo_worker import current_cpu_affinity, pin_cpu_core, set_cpu_affinity
from ring1_spinal.spinal_loop import ZeroGCSpinalEngine

SYMBOL = "BTCUSDT"
STRATEGY = "phase4-latency-probe"


def flow_tick(valid_ns: int) -> ExecutedFlowTick:
    return ExecutedFlowTick(
        symbol=SYMBOL,
        valid_time_ns=valid_ns,
        knowledge_ts_ns=valid_ns,
        last_price=100.0,
        spread_bps=2.0,
        realized_vol_5s=1.0,
        normal_vol_5s=1.0,
        taker_buy_volume_5s=710.0,
        taker_sell_volume_5s=290.0,
        bid_cancel_to_fill_ratio=1.4,
        ask_cancel_to_fill_ratio=1.4,
    )


unlink_all()
kill = GlobalKillLatch(reset=True)
leases = LeaseTableSHM(reset=True, kill_latch=kill)
book = ShadowBookSHM(reset=True)
available = current_cpu_affinity() or (0,)
pin = pin_cpu_core(available[-1])
ring1_p50 = ring1_p99 = float("nan")
try:
    base = time.monotonic_ns()
    lease = System1Lease(
        symbol=SYMBOL,
        mode=LeaseMode.LONG_ONLY,
        target_bps=18.0,
        stop_bps=12.0,
        expires_at_ns=base + LEASE_TTL_NS,
        strategy_id_hash=7,
    )
    leases.write_lease(lease, now_ns=base)
    book.write_snapshot(SYMBOL, 100.0, 100.02, 500.0, 500.0, updated_at_ns=base)
    twin = ShadowExecutionTwin(book)
    warm = ZeroGCSpinalEngine(
        leases, kill_latch=kill, shadow_twin=twin, bus=None, strategy_id=STRATEGY,
        proposal_sink=[], card_sink=[],
    )
    for index in range(REQUIRED_SHADOW_FILLS + 1):
        warm.on_tick(flow_tick(base + index), now_ns=base + index)

    probe = ZeroGCSpinalEngine(
        leases, kill_latch=kill, shadow_twin=twin, bus=None, strategy_id=STRATEGY,
        proposal_sink=[], card_sink=[],
    )
    window = [flow_tick(base + 10_000 + i) for i in range(1_000)]
    samples: list[float] = []
    with probe.gc_guard():
        for index, tick in enumerate(window):
            begin = time.perf_counter_ns()
            probe.on_tick(tick, now_ns=base + 10_000 + index)
            samples.append((time.perf_counter_ns() - begin) / 1e6)
    ring1_p50, ring1_p99 = pct(samples, 0.50), pct(samples, 0.99)

    # Tier-0 shared-memory lease read.
    reads: list[float] = []
    for _ in range(2_000):
        begin = time.perf_counter_ns()
        leases.read_lease(SYMBOL, now_ns=base)
        reads.append((time.perf_counter_ns() - begin) / 1e6)
    shm_p50, shm_p99 = pct(reads, 0.50), pct(reads, 0.99)
finally:
    if pin.pinned and pin.previous:
        try:
            set_cpu_affinity(pin.previous)
        except Exception:
            pass
    for handle in (book, leases, kill):
        try:
            handle.close()
        except Exception:
            pass
        try:
            handle.unlink()
        except Exception:
            pass
    unlink_all()

add(
    "Ring 1 tick-to-intent (p50)",
    "< 0.088 ms",
    f"{ring1_p50:.4f} ms",
    ring1_p50 < 0.088,
    "1000 ticks, pinned core, GC guard",
)
add(
    "Ring 1 tick-to-intent (p99)",
    "< 0.140 ms",
    f"{ring1_p99:.4f} ms",
    ring1_p99 < 0.140,
    "1000 ticks, pinned core, GC guard",
)
add(
    "Tier-0 SHM lease read (p50/p99)",
    "< 0.005 / 0.008 ms",
    f"{shm_p50:.5f} / {shm_p99:.5f} ms",
    shm_p50 < 0.005 and shm_p99 < 0.008,
    "2000 SeqLock reads",
)

# ---------------------------------------------------------------------------
# 2. MiroFish 3-tick cascade + MCP tool backend
# ---------------------------------------------------------------------------
from ring2_brain.mirofish.contagion_engine import MiroFishContagionEngine, ShockVector

engine = MiroFishContagionEngine(silo="US", warmup=True)
shock = ShockVector(
    price_shock_bps=40.0, realized_vol_ratio=2.0, spread_bps=6.0,
    dealer_gamma_flip_proximity=0.2,
)
cascade_ms: list[float] = []
result = None
for _ in range(50):
    begin = time.perf_counter_ns()
    result = engine.run(shock, "BTCUSDT", persist=False)
    cascade_ms.append((time.perf_counter_ns() - begin) / 1e6)
c_med, c_p95 = pct(cascade_ms, 0.50), pct(cascade_ms, 0.95)
add(
    "MiroFish 3-tick cascade (median)",
    "< 1.8 ms",
    f"{c_med:.4f} ms",
    c_med < 1.8,
    f"5 archetypes x 3 ticks, fragility={result.features.cascade_fragility_index:.3f}",
)
add(
    "MiroFish 3-tick cascade (p95)",
    "< 1.8 ms",
    f"{c_p95:.4f} ms",
    c_p95 < 1.8,
    "50 consecutive cascades",
)

# ---------------------------------------------------------------------------
# 3. Signed 22-gate certificate rows
# ---------------------------------------------------------------------------
completed = subprocess.run(
    [PY, "-X", "utf8", str(ROOT / "scripts" / "preflight_golive.py"), "--fast", "--json"],
    cwd=str(ROOT), capture_output=True, text=True, check=False,
)
report = json.loads(completed.stdout)
gates = {gate["gate"]: gate for gate in report["gates"]}

kill_ev = gates["cockpit.killswitch_and_rearm"]["evidence"]
kill_ns = int(kill_ev.get("latency_ns", 0))
add(
    "Global kill switch",
    "< 1.0 ms",
    f"{kill_ns / 1e6:.4f} ms",
    0 < kill_ns < 1_000_000,
    f"{kill_ev.get('halted_state')} -> {kill_ev.get('rearm_state')}",
)

contagion_ev = gates["ring2.contagion_three_tick_cascade"]["evidence"]
add(
    "Gate 15 contagion SLA verdict",
    "< 1.8 ms",
    f"{float(contagion_ev.get('cascade_fragility_index', 0)) and ''}"
    f"within_sla={contagion_ev.get('within_sla')}",
    bool(contagion_ev.get("within_sla")),
    f"SLA {contagion_ev.get('sla_ms')} ms",
)

obs_ev = gates["observability.metrics_and_health"]["evidence"]
machine_keys = obs_ev.get("json_keys") or []
add(
    "Prometheus / OTel metrics + cockpit health",
    "all scrapes 200",
    f"/metrics={obs_ev.get('prometheus_status')} "
    f"json={obs_ev.get('json_status')} health={obs_ev.get('health_status')}",
    gates["observability.metrics_and_health"]["status"] == "PASS",
    f"{len(machine_keys)} machine metric keys, health={obs_ev.get('health_state')}",
)

print()
print("NIOS v3.0.0-PROD -- SECTION 4.3 LATENCY SCORECARD")
head = subprocess.run(
    ["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT), capture_output=True, text=True
).stdout.strip()
print(
    f"commit {head}  annotated tag v3.0.0-PROD  "
    f"certificate {report['certificate_sha256'][:12]}..."
)
print()
header = f"{'CAPABILITY':<46}{'BUDGET':<24}{'MEASURED':<32}{'VERDICT':<10}EVIDENCE"
print(header)
print("-" * (len(header) + 8))
for capability, budget, measured, status, evidence in ROWS:
    print(f"{capability:<46}{budget:<24}{measured:<32}{status:<10}{evidence}")
print("-" * (len(header) + 8))
fails = sum(1 for row in ROWS if row[3] == "FAIL")
warns = sum(1 for row in ROWS if row[3] == "WARN")
print(f"ROWS={len(ROWS)}  PASS={len(ROWS) - fails - warns}  WARN={warns}  FAIL={fails}")
print(
    "FAIL rows (X) = "
    f"{fails}   WARN rows (!) = {warns}   "
    f"-> {fails} X, {warns} !"
)
print(
    f"GATES={report['gates_passed']}/{report['gates_expected']}  "
    f"certificate_sha256={report['certificate_sha256']}"
)
sys.exit(0 if fails == 0 else 1)
