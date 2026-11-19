"""V2.3 PART 3 verification: the six gateway telemetry fields are real.

Spins the real FastAPI app on a throwaway data root and proves:

1. ``GET /api/v2/status`` publishes a non-null ``latency_ms`` block plus every
   per-ring ``p99`` key, with the numbers coming from measured spans;
2. ``feed_lag_ms`` is ``None`` with ``feed_lag_status == "IDLE"`` before any tick;
3. after one injected bar the same field is a real number and status is ``LIVE``;
4. ``pending_intents_count`` / ``adwin_drift`` / ``iv_rank_bps`` are present on
   the surfaces the spec names.

Run:  python scripts/verify_v23_telemetry.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from core.orchestrator import NIOSCoreOrchestrator  # noqa: E402
from core.telemetry import reset_gateway_telemetry  # noqa: E402
from ring3_cockpit.gateway.control_plane import create_app  # noqa: E402

RING_P99_KEYS = ("ring0_p99_ms", "ring1_p99_ms", "ring2_p99_ms", "ring3_p99_ms")


def _show(label: str, payload: dict) -> None:
    subset = {
        key: payload.get(key)
        for key in (
            "latency_ms",
            "ring0_p99_ms",
            "ring1_p99_ms",
            "ring2_p99_ms",
            "ring3_p99_ms",
            "latency_p50_ms",
            "latency_p95_ms",
            "latency_ring_samples",
            "feed_lag_ms",
            "feed_lag_status",
            "pending_intents_count",
            "adwin_drift",
            "iv_rank_bps",
        )
    }
    print(f"\n[{label}]")
    print(json.dumps(subset, indent=1, default=str))


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="nios-v23-telemetry-") as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        vault = root / "vault"
        workspace.mkdir(parents=True, exist_ok=True)
        vault.mkdir(parents=True, exist_ok=True)
        runtime = NIOSCoreOrchestrator(
            workspace=workspace, key_vault_root=vault, clean_slate=True
        )
        reset_gateway_telemetry()
        try:
            with TestClient(create_app(runtime.control)) as client:
                status = client.get("/api/v2/status")
                if status.status_code != 200:
                    failures.append(f"/api/v2/status -> {status.status_code}")
                body = status.json()
                _show("GET /api/v2/status (cold, before any tick)", body)

                if not isinstance(body.get("latency_ms"), (int, float)):
                    failures.append("latency_ms is not a number")
                else:
                    detail = body.get("latency_detail") or {}
                    for key in ("last", "p50", "p95", "p99"):
                        if detail.get(key) is None:
                            failures.append(f"latency_detail.{key} is null")
                    if int(detail.get("count") or 0) < 1:
                        failures.append("latency_detail.count < 1")
                for key in RING_P99_KEYS:
                    if key not in body:
                        failures.append(f"{key} missing")
                if body.get("ring3_p99_ms") is None:
                    failures.append("ring3_p99_ms is null after one request")
                if body.get("ring1_p99_ms") is None:
                    failures.append("ring1_p99_ms is null (SHM reads not measured)")
                if body.get("feed_lag_ms") is not None:
                    failures.append("feed_lag_ms should be null before any tick")
                if body.get("feed_lag_status") != "IDLE":
                    failures.append("feed_lag_status should be IDLE before any tick")
                if not isinstance(body.get("pending_intents_count"), int):
                    failures.append("pending_intents_count is not an int")

                overview = client.get("/api/v2/cockpit/overview")
                if overview.status_code != 200:
                    failures.append(f"/api/v2/cockpit/overview -> {overview.status_code}")
                supervisor = client.get("/api/v2/control/supervisor")
                if supervisor.status_code != 200:
                    failures.append(f"/api/v2/control/supervisor -> {supervisor.status_code}")
                else:
                    sup = supervisor.json()
                    if "ring3_p99_ms" not in sup:
                        failures.append("supervisor payload lacks ring3_p99_ms")

                # -- inject one real tick, then re-read the feed telemetry -----
                tick = client.post(
                    "/api/v2/control/market/tick",
                    json={
                        "silo": "CRYPTO_BINANCE",
                        "symbol": "BTCUSDT",
                        "price": 68_250.5,
                        "size": 0.75,
                    },
                )
                if tick.status_code not in (200, 401, 403):
                    failures.append(f"market/tick -> {tick.status_code}: {tick.text[:160]}")

                after = client.get("/api/v2/status").json()
                _show("GET /api/v2/status (after one injected bar)", after)
                if after.get("feed_lag_status") != "LIVE":
                    failures.append(
                        "feed_lag_status is not LIVE after an ingested bar "
                        f"(got {after.get('feed_lag_status')!r}); "
                        "the tick POST may have been rejected by the session guard"
                    )
                if not isinstance(after.get("feed_lag_ms"), (int, float)):
                    failures.append("feed_lag_ms is not numeric after an ingested bar")
                for key in RING_P99_KEYS:
                    if after.get(key) is None:
                        failures.append(f"{key} still null after real activity")

                # -- iv_rank_bps + adwin_drift surfaces -----------------------
                chain = client.get("/api/v2/cockpit/options")
                if chain.status_code != 200:
                    failures.append(f"/api/v2/cockpit/options -> {chain.status_code}")
                instrument = client.get("/api/v2/instruments/BTCUSDT")
                if instrument.status_code != 200:
                    failures.append(
                        f"/api/v2/instruments/BTCUSDT -> {instrument.status_code}"
                    )
                elif "iv_rank_bps" not in instrument.json():
                    failures.append("instrument detail lacks iv_rank_bps")

                # Price the same chain three times at three real volatilities so
                # the trailing IV window has a low, a high and a current point.
                ranks: list[dict] = []
                for vol in (0.12, 0.45, 0.25):
                    priced = client.post(
                        "/api/v2/control/options/chain",
                        json={"symbol": "NIFTY", "volatility": vol},
                    )
                    if priced.status_code != 200:
                        failures.append(
                            f"options/chain({vol}) -> {priced.status_code}: "
                            f"{priced.text[:160]}"
                        )
                        break
                    ranks.append(priced.json())
                if len(ranks) == 3:
                    print(
                        "\n[iv_rank_bps across three priced NIFTY chains]",
                        [row.get("iv_rank_bps") for row in ranks],
                    )
                    if ranks[0].get("iv_rank_bps") is not None:
                        failures.append("first chain should have no IV history yet")
                    final_rank = ranks[-1].get("iv_rank_bps")
                    if not isinstance(final_rank, int) or not 0 <= final_rank <= 10_000:
                        failures.append(
                            f"iv_rank_bps not an int in 0..10000: {final_rank!r}"
                        )
                    detail = client.get("/api/v2/instruments/NIFTY").json()
                    if not isinstance(detail.get("iv_rank_bps"), int):
                        failures.append(
                            "instrument detail iv_rank_bps not an int after history"
                        )
                    else:
                        print(
                            "[instrument NIFTY iv_rank_bps]",
                            detail["iv_rank_bps"],
                            "samples",
                            detail["iv_rank_detail"]["iv_window_samples"],
                        )

                # -- a paper order must produce a real Ring 0 span + intent ----
                before_ring0 = client.get("/api/v2/status").json()["ring0_p99_ms"]
                funding = client.post(
                    "/api/v2/control/ledger/fund-paper",
                    json={
                        "silo": "CRYPTO_BINANCE",
                        "amount_units": 50_000.0,
                        "vault": "SpinalScalper",
                    },
                )
                if funding.status_code != 200:
                    failures.append(f"ledger/fund-paper -> {funding.status_code}")
                paper = client.post(
                    "/api/v2/control/orders/paper",
                    json={
                        "silo": "CRYPTO_BINANCE",
                        "symbol": "BTCUSDT",
                        "side": "BUY",
                        "quantity": 0.01,
                        "expected_gross_bps": 90.0,
                        "order_style": "MAKER_PEG",
                        "fill_immediately": False,
                    },
                )
                print("\n[paper order]", paper.status_code)
                if paper.status_code != 200:
                    failures.append(
                        f"orders/paper -> {paper.status_code}: {paper.text[:200]}"
                    )
                post_status = client.get("/api/v2/status").json()
                if post_status["latency_ring_samples"]["ring0"] < 1:
                    failures.append("ring0 never recorded a span")
                if post_status["pending_intents_count"] < 1:
                    failures.append(
                        "pending_intents_count did not count the working order's "
                        "pending outbox reservation"
                    )
                print(
                    "[ring0 samples] before",
                    before_ring0,
                    "after",
                    post_status["latency_ring_samples"]["ring0"],
                    "| pending intents",
                    post_status["pending_intents_count"],
                    post_status["pending_intents"],
                )

                # -- a closed trade must feed the ADWIN drift monitor ----------
                closed = client.post(
                    "/api/v2/control/trades/close",
                    json={
                        "silo": "CRYPTO_BINANCE",
                        "symbol": "BTCUSDT",
                        "gross_pnl_micros": 900_000,
                        "notional_micros": 50_000_000,
                        "side": "SELL",
                    },
                )
                print("[trade close]", closed.status_code)
                if closed.status_code != 200:
                    failures.append(
                        f"trades/close -> {closed.status_code}: {closed.text[:200]}"
                    )
                forge = client.get("/api/v2/cockpit/forge")
                if forge.status_code != 200:
                    failures.append(f"/api/v2/cockpit/forge -> {forge.status_code}")
                else:
                    forge_body = forge.json()
                    drift = forge_body.get("adwin_drift")
                    if not isinstance(drift, dict) or "state" not in drift:
                        failures.append("forge payload lacks an adwin_drift object")
                    else:
                        print(
                            "\n[forge adwin_drift]",
                            json.dumps(drift, indent=1)[:600],
                        )
                        if int(drift.get("window_size") or 0) < 1:
                            failures.append(
                                "adwin_drift.window_size is 0 after a closed trade"
                            )
                        if drift.get("last_checked_utc") is None:
                            failures.append(
                                "adwin_drift.last_checked_utc is null after a close"
                            )
                    if "adwin_drift_by_strategy" not in forge_body:
                        failures.append("forge payload lacks adwin_drift_by_strategy")
                    if not all(
                        isinstance(row.get("adwin_drift"), dict)
                        for row in forge_body.get("leaderboard", [])
                    ):
                        failures.append("a leaderboard row lacks its adwin_drift block")
        finally:
            try:
                runtime.control.reset_workspace()
            except Exception:
                pass
            closer = getattr(runtime, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass

    if failures:
        print("\nFAILURES:")
        for item in failures:
            print(f" - {item}")
        return 1
    print("\nALL TELEMETRY ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
