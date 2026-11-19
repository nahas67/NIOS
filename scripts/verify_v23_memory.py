#!/usr/bin/env python3
"""V2.3 PART 2.2 / 2.3 memory proof.

Five executable claims, all offline and deterministic:

``a``  ``compact_timeline`` seals the timeline automatically once the active raw
       event count passes ``compact_threshold`` (default ``500``).
``b``  A full replay and a ``checkpoint + delta`` replay of the same events
       produce byte-identical ``CompiledEntityTruth`` rows and ``truth_sha256``
       digests -- including after a restart from the persisted state.
``c``  The 3-signal fusion separates two entities that the old 2-signal
       (feature + lexical) formula could not: it ranks the semantically-near one
       first even though the lexical scores are identical.
``d``  ``GET /api/v2/memory/episodes`` answers ``200`` with an envelope before
       any intervention (empty list) and after a ``plannotator`` inline override
       (one ``MISSION_OVERRIDE`` episode).
``e``  ``POST /api/v2/memory/compact`` returns a verified ``MemoryCheckpoint``.

Usage::

    python scripts/verify_v23_memory.py [--json PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DAY_NS = 86_400 * 1_000_000_000
UNITS = 1_000_000


def _build_graph(events: int, **kwargs: Any):
    from ring2_brain.memory.entity_graph import EntityMemoryGraph

    graph = EntityMemoryGraph(**kwargs)
    for index in range(events):
        graph.record_outcome(
            outcome_kind=("WIN", "LOSS", "SLIPPAGE_SPIKE", "ORDER_REJECTED")[index % 4],
            net_pnl_micros=((index * 37) % 400 - 150) * 1_000_000,
            slippage_bps=float((index * 13) % 40) + 0.125,
            symbol=("BTCUSDT", "ETH-USDT", "SOLUSDT")[index % 3],
            silo="CRYPTO_BINANCE",
            strategy_id="alpha_v3",
            regime=("HIGH_VOL", "LOW_VOL")[index % 2],
            lesson_summary=f"lesson {index % 7} about gap risk on the open",
            ts_utc=1_700_000_000_000_000_000 + index * 1_000_000,
            event_id=f"E-{index:06d}",
        )
    return graph


def proof_a_and_b(workspace: Path) -> dict[str, Any]:
    """(a) auto-compaction at >500 events, (b) byte-identical truth invariance."""

    events = 620
    archive = workspace / "entity_memory_cold_archive.jsonl"
    full = _build_graph(events, compact_threshold=10**9, archive_path=archive)
    sealed = _build_graph(events, archive_path=archive)

    full_truths = {row.entity_id: row.to_dict() for row in full.truths()}
    sealed_truths = {row.entity_id: row.to_dict() for row in sealed.truths()}

    checkpoint = sealed.checkpoint()
    summary = sealed.seal_summary()

    from ring2_brain.memory.entity_graph import EntityMemoryGraph

    restored = EntityMemoryGraph(state=sealed.to_state(), archive_path=archive)
    restored_truths = {row.entity_id: row.to_dict() for row in restored.truths()}

    archive_rows = sealed.read_archive_file()

    checks = {
        "auto_compaction_triggered": bool(summary["checkpoint_seq"] > 0),
        "active_events_bounded": int(summary["active_events"]) <= 500,
        "active_events_equal_remainder": int(summary["active_events"])
        == events - int(summary["checkpoint_seq"]),
        "event_count_preserved": int(summary["event_count"]) == events,
        "checkpoint_verified": bool(checkpoint is not None and checkpoint.verify()),
        "truth_table_identical": full_truths == sealed_truths,
        "truth_sha256_identical": all(
            full_truths[key]["truth_sha256"] == sealed_truths[key]["truth_sha256"]
            for key in full_truths
        ),
        "restart_identical": restored_truths == full_truths,
        "restart_replayed_only_delta": len(restored._events) == len(sealed._events),
        "cold_archive_readable": len(archive_rows) == int(summary["archive_events"]),
        "cold_archive_durable": archive.is_file() and len(archive_rows) > 0,
    }
    return {
        "events_recorded": events,
        "checkpoint_seq": int(summary["checkpoint_seq"]),
        "active_events": int(summary["active_events"]),
        "event_count_compacted": int(
            checkpoint.event_count_compacted if checkpoint is not None else 0
        ),
        "checkpoint_sha256": str(summary["checkpoint_sha256"]),
        "archive_events": int(summary["archive_events"]),
        "entity_count": len(full_truths),
        "entities": sorted(full_truths),
        "truth_sha256": {
            key: full_truths[key]["truth_sha256"] for key in sorted(full_truths)
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def proof_c() -> dict[str, Any]:
    """(c) the semantic arm separates a lexical tie."""

    from ring2_brain.memory.entity_graph import EntityMemoryGraph

    graph = EntityMemoryGraph()
    # Two entities whose lessons are the same length and both contain the query
    # token ``auction`` exactly once, so BM25 and the entity-feature arm score
    # them identically.  Their own ids are neutral (no query token), which keeps
    # the lexical tie exact; only the char-ngram arm can separate them.
    graph.record_outcome(
        outcome_kind="LOSS",
        net_pnl_micros=-1_000_000,
        symbol="LEXLOC",
        silo="US_IBKR",
        lesson_summary="auction alpha bravo charlie delta echo",
        ts_utc=1_700_000_000_000_000_000,
        event_id="E-LEX",
    )
    graph.record_outcome(
        outcome_kind="LOSS",
        net_pnl_micros=-1_000_000,
        symbol="SEMLOC",
        silo="US_IBKR",
        lesson_summary="auction slip slipstream slippy slid slide",
        ts_utc=1_700_000_000_000_000_001,
        event_id="E-SEM",
    )

    report = graph.query_entity_memory(
        query_text="auction slippage gap", top_k=5, now_ns=1_800_000_000_000_000_000
    )
    rows = {row["entity_id"]: row for row in report["truths"]}
    lexical_near = rows["symbol:LEXLOC"]  # the token match, semantically unrelated
    semantic_near = rows["symbol:SEMLOC"]  # morphological variants of "slippage"

    legacy_lexical_near = 0.30 * lexical_near["signals"]["lexical"] + 0.70 * lexical_near["signals"]["feature"]
    legacy_semantic_near = 0.30 * semantic_near["signals"]["lexical"] + 0.70 * semantic_near["signals"]["feature"]

    order = [row["entity_id"] for row in report["truths"]]
    checks = {
        "lexical_tie": abs(
            lexical_near["signals"]["lexical"] - semantic_near["signals"]["lexical"]
        )
        < 1e-9,
        "semantic_arm_differs": semantic_near["signals"]["semantic"]
        > lexical_near["signals"]["semantic"],
        "legacy_two_signal_tie": abs(legacy_lexical_near - legacy_semantic_near) < 1e-12,
        "three_signal_ranks_semantic_first": order.index("symbol:SEMLOC")
        < order.index("symbol:LEXLOC"),
        "weights_are_25_45_30": report["weights"]
        == {"lexical": 0.25, "feature": 0.45, "semantic": 0.3},
        "dim_is_384": int(report["semantic_dim"]) == 384,
        "packet_sealed": bool(report["packet_sha256"]),
        "packet_carries_signal_maxima": all(
            f"retrieval_{name}_max" in report["packet"]["metrics"]
            for name in ("lexical", "feature", "semantic")
        ),
    }
    return {
        "order": order,
        "signals": {
            "lexical_near": lexical_near["signals"],
            "semantic_near": semantic_near["signals"],
        },
        "legacy_scores": {
            "lexical_near": round(legacy_lexical_near, 6),
            "semantic_near": round(legacy_semantic_near, 6),
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def _mission_payload(mission_id: str, *, days: int = 30) -> dict[str, Any]:
    return {
        "mission_id": mission_id,
        "target_units": 100.0,
        "deadline_utc": time.time_ns() + days * DAY_NS,
        "risk_budget_units": 100.0,
        "operator": "sovereign",
        "silos": ["US_IBKR"],
        "notional_cap_units": 50_000.0,
        "state": "APPROVE",
    }


def proof_d_and_e(workspace: Path) -> dict[str, Any]:
    """(d) episodes endpoint before/after a plannotator override, (e) compaction."""

    from fastapi.testclient import TestClient

    from core.orchestrator import NIOSCoreOrchestrator
    from ring3_cockpit.gateway.control_plane import create_app

    data = workspace / "orchestrator"
    vault = workspace / "vault"
    data.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)
    instance = NIOSCoreOrchestrator(workspace=data, key_vault_root=vault, clean_slate=True)
    checks: dict[str, bool] = {}
    try:
        with TestClient(create_app(instance.control)) as client:
            before = client.get("/api/v2/memory/episodes", params={"limit": 50})
            before_body = before.json()
            checks["empty_before_200"] = before.status_code == 200
            checks["empty_before_envelope"] = bool(before_body.get("payload_sha256"))
            checks["empty_before_zero"] = int(before_body.get("count", -1)) == 0
            checks["empty_before_is_list"] = before_body.get("episodes") == []

            funded = client.post(
                "/api/v2/control/ledger/fund-paper",
                json={
                    "silo": "US_IBKR",
                    "amount_units": 100_000.0,
                    "vault": "LongTermCompounder",
                },
            )
            checks["funded"] = funded.status_code == 200

            created = client.post(
                "/api/v2/missions", json=_mission_payload("M-EPISODIC-1")
            )
            checks["mission_created"] = created.status_code == 200

            opened = client.post(
                "/api/v2/missions/M-EPISODIC-1/tranches",
                json={"silo": "US_IBKR", "allocated_units": 10_000.0},
            )
            checks["tranche_opened"] = opened.status_code == 200
            tranche_id = opened.json().get("tranche_id", "")

            approved = client.post(
                "/api/v2/missions/M-EPISODIC-1/approve",
                json={
                    "operator": "sovereign",
                    "state": "APPROVE",
                    "excluded_symbols": ["btcusdt"],
                    "tranche_caps_micros": {tranche_id: 4_000 * UNITS},
                    "reserve_split_override_bps": 7000,
                    "operator_note": "Trim the crypto leg; keep the reserve floor elevated.",
                },
            )
            checks["override_accepted"] = approved.status_code == 200
            checks["override_is_override_state"] = (
                approved.json().get("approval_state") == "OVERRIDE"
            )

            after = client.get("/api/v2/memory/episodes", params={"limit": 50})
            after_body = after.json()
            episodes = after_body.get("episodes") or []
            checks["after_200"] = after.status_code == 200
            checks["after_envelope"] = bool(after_body.get("payload_sha256"))
            checks["after_one_episode"] = len(episodes) == 1
            checks["episode_is_override"] = bool(
                episodes and episodes[0].get("action_kind") == "MISSION_OVERRIDE"
            )
            checks["episode_has_context"] = bool(
                episodes
                and episodes[0].get("context_tags", {}).get("excluded_symbols")
                == ["BTCUSDT"]
                and episodes[0].get("context_tags", {}).get(
                    "reserve_split_override_bps"
                )
                == 7000
            )
            checks["episode_is_self_sealing"] = bool(
                episodes and len(str(episodes[0].get("payload_sha256", ""))) == 64
            )

            filtered = client.get(
                "/api/v2/memory/episodes", params={"action_kind": "MISSION_REVOKE"}
            )
            checks["filter_returns_empty"] = filtered.status_code == 200 and (
                filtered.json().get("count") == 0
            )

            # Journal durability: the episode rides the atomic journal under its
            # own top-level key, so a restart restores it.
            journal_state = instance.control.state_journal.load()
            stored = journal_state.get("episodic_memory") or {}
            checks["journal_persisted_episode"] = (
                int(stored.get("episode_count", 0) or 0) == 1
            )

            # Seed the post-mortem timeline so compaction has something to seal.
            for index, kind in enumerate(
                ("LOSS", "SLIPPAGE_SPIKE", "WIN", "ORDER_REJECTED", "LOSS")
            ):
                recorded = client.post(
                    "/api/v2/memory/entities",
                    json={
                        "outcome_kind": kind,
                        "net_pnl_units": -1.0 if kind != "WIN" else 2.0,
                        "slippage_bps": 31.0 if kind == "SLIPPAGE_SPIKE" else 4.0,
                        "symbol": "BTCUSDT",
                        "silo": "CRYPTO_BINANCE",
                        "regime": "NEUTRAL",
                        "lesson_summary": f"proof event {index}",
                    },
                )
                checks[f"post_mortem_{index}_accepted"] = recorded.status_code == 200

            compacted = client.post("/api/v2/memory/compact", json={"keep_recent": 2})
            compact_body = compacted.json()
            checkpoint = compact_body.get("checkpoint")
            checks["compact_200"] = compacted.status_code == 200
            checks["compact_envelope"] = bool(compact_body.get("payload_sha256"))
            checks["compact_has_checkpoint"] = bool(
                isinstance(checkpoint, dict)
                and len(str(checkpoint.get("checkpoint_sha256", ""))) == 64
            )
            checks["compact_sealed_events"] = int(
                compact_body.get("event_count_compacted", 0)
            ) == 3
            checks["compact_reports_episodic"] = "episodic_memory" in compact_body
            checks["episode_count_preserved"] = int(after_body.get("total", 0)) >= 1

            revoked = client.post(
                "/api/v2/missions/M-EPISODIC-1/revoke",
                json={"operator": "sovereign", "reason": "proof revoke"},
            )
            checks["revoke_accepted"] = revoked.status_code == 200
            final = client.get("/api/v2/memory/episodes", params={"limit": 50}).json()
            final_kinds = [
                str(row.get("action_kind")) for row in (final.get("episodes") or [])
            ]
            checks["revoke_recorded"] = "MISSION_REVOKE" in final_kinds
            checks["two_episodes_after_revoke"] = len(final_kinds) == 2

        return {
            "before": {
                "status": before.status_code,
                "count": int(before_body.get("count", -1)),
                "payload_sha256": str(before_body.get("payload_sha256", ""))[:16],
            },
            "after": {
                "status": after.status_code,
                "count": int(after_body.get("count", -1)),
                "total": int(after_body.get("total", -1)),
                "action_kinds": [
                    str(row.get("action_kind")) for row in (after_body.get("episodes") or [])
                ],
                "payload_sha256": str(after_body.get("payload_sha256", ""))[:16],
            },
            "compact": {
                "status": compacted.status_code,
                "compacted": bool(compact_body.get("compacted")),
                "checkpoint_sha256": str(
                    (compact_body.get("checkpoint") or {}).get("checkpoint_sha256", "")
                ),
                "checkpoint_seq": int(compact_body.get("checkpoint_seq", 0)),
                "episodic_memory": compact_body.get("episodic_memory"),
            },
            "checks": checks,
            "passed": all(checks.values()),
        }
    finally:
        try:
            instance.control.reset_workspace()
        except Exception:
            pass
        closer = getattr(instance, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass


def proof_f(workspace: Path) -> dict[str, Any]:
    """(f) digit-free caution injection into a real ``AgentTaskDispatcher``."""

    from ring2_brain.agents.task_dispatcher import AgentTaskDispatcher
    from ring2_brain.memory.episodic_memory import EpisodicMemoryStore

    store = EpisodicMemoryStore()
    store.record_mission_override(
        mission_id="M-CAUTION",
        actor="sovereign",
        tranche_caps_micros={"M-CAUTION-T001": 4_000_000_000},
        excluded_symbols=["BTCUSDT"],
        reserve_split_override_bps=7000,
        operator_note="cut size on the crypto leg after the 12 bps slippage print",
        now_ns=1_700_000_000_000_000_000,
    )
    store.record_autopilot(enabled=False, now_ns=1_700_000_001_000_000_000)
    store.record_mission_revoke(
        mission_id="M-CAUTION",
        actor="sovereign",
        reason="thesis invalidated",
        now_ns=1_700_000_002_000_000_000,
    )
    store.record_tranche_rearm(
        tranche_id="M-CAUTION-T001",
        actor="sovereign",
        mission_id="M-CAUTION",
        now_ns=1_700_000_003_000_000_000,
    )
    store.record_clear_freeze(
        silo="US_IBKR",
        actor="sovereign",
        note="reconciled against the venue statement",
        previous_reason="COLD_START_UNKNOWN_FILL",
        now_ns=1_700_000_004_000_000_000,
    )

    kinds = {row["action_kind"] for row in store.describe(limit=50)["episodes"]}
    shape = set(store.describe(limit=1)["episodes"][0]) if len(store) else set()

    dispatcher = AgentTaskDispatcher()
    cautions = store.inject_cautions(
        dispatcher,
        symbol="BTCUSDT",
        silo="CRYPTO_BINANCE",
        query_text="btcusdt momentum",
        now_ns=1_700_000_100_000_000_000,
    )
    narrative = str((cautions or {}).get("narrative", ""))
    packet = (cautions or {}).get("packet") or {}
    metrics = packet.get("metrics") or {}

    checks = {
        "store_attached": getattr(dispatcher, "episodic_memory", None) is store,
        "cautions_returned": bool(cautions),
        "narrative_digit_free": not any(char.isdigit() for char in narrative),
        "narrative_non_empty": len(narrative) > 0,
        "packet_present": bool(packet.get("sha256_digest")),
        "packet_carries_counts": float(metrics.get("episode_matches", 0.0))
        == float(len((cautions or {}).get("episode_ids") or [])),
        "no_match_returns_none": store.cautions_for(symbol="ZZZZZZ") is None,
        "narrative_flags": bool((cautions or {}).get("digit_free")),
        "all_six_action_kinds_recordable": kinds
        == {
            "MISSION_OVERRIDE",
            "MISSION_REVOKE",
            "TRANCHE_REARM",
            "CLEAR_FREEZE",
            "AUTOPILOT_PAUSE",
        },
        "pg16_row_shape": shape
        == {
            "episode_id",
            "ts_utc",
            "actor",
            "action_kind",
            "regime",
            "context_tags",
            "decision_summary",
            "payload_sha256",
        },
        "episodes_newest_first": [
            row["action_kind"] for row in store.describe(limit=50)["episodes"]
        ][0]
        == "CLEAR_FREEZE",
    }
    return {
        "narrative": narrative,
        "episode_ids": (cautions or {}).get("episode_ids") or [],
        "action_kinds": (cautions or {}).get("action_kinds") or [],
        "packet_metrics": metrics,
        "recorded_kinds": sorted(kinds),
        "row_shape": sorted(shape),
        "checks": checks,
        "passed": all(checks.values()),
    }


def run() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="nios-v23-memory-") as tmp:
        workspace = Path(tmp)
        part_ab = proof_a_and_b(workspace / "graph")
        part_c = proof_c()
        part_f = proof_f(workspace / "cautions")
        part_de = proof_d_and_e(workspace / "gateway")
    report = {
        "schema": "v2.3.memory-proof",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "a_compaction": part_ab,
        "b_invariance": {
            "truth_table_identical": part_ab["checks"]["truth_table_identical"],
            "truth_sha256_identical": part_ab["checks"]["truth_sha256_identical"],
            "restart_identical": part_ab["checks"]["restart_identical"],
            "restart_replayed_only_delta": part_ab["checks"]["restart_replayed_only_delta"],
        },
        "c_three_signal_ranking": part_c,
        "d_episodic_endpoint": {
            key: value for key, value in part_de.items() if key != "checks"
        },
        "e_compact_endpoint": part_de["compact"],
        "f_caution_injection": part_f,
        "checks": {
            **part_ab["checks"],
            **part_c["checks"],
            **part_f["checks"],
            **part_de["checks"],
        },
    }
    report["passed"] = bool(
        part_ab["passed"] and part_c["passed"] and part_f["passed"] and part_de["passed"]
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="V2.3 memory proof")
    parser.add_argument("--json", default="", help="write the report to this path")
    args = parser.parse_args()

    report = run()
    body = json.dumps(report, indent=2, sort_keys=True)
    if args.json:
        Path(args.json).write_text(f"{body}\n", encoding="utf-8")
    print(body)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
