"""V1.4 - clean-room masterpiece frontend rebuild.

Gate inventory for the clean-room UI release (``v3.5.0-V1.4``):

1. The old ``src/`` tree and ``dist/`` are gone; the new architecture exists
   exactly where the V1.4 blueprint says it does.
2. All fifteen pages across ``/``, ``/paper/*`` and ``/live/*`` plus the five
   auth surfaces answer ``200 text/html`` from the built ``dist/`` bundle.
3. Paper and live shells are physically and functionally separated: the live
   surface can never render a paper deposit, a shadow burst or a workspace
   reset, and it always renders the hazard ribbon, the 30-minute lease, the
   22-gate chip and the two-step kill switch.
4. ``/live/login`` is a true three-factor door (password + six-digit PIN +
   explicit real-capital acknowledgement).
5. The router, the page catalog, the five-domain sidebar and the ``Ctrl/Cmd+K``
   palette all cover the same fifteen routes.
6. Every page is a bespoke workstation (SectionHeader + panels), never a
   generic key/value dump and never the removed ``PageShell``.
7. A pristine install renders the clean slate: zero vaults, zero orders, zero
   logbook rows, a 0/30 nursery and an unarmed autopilot.
8. ``npx biome lint src/`` is clean and the bundle still carries the four
   string markers asserted by the V1.3 regression gate.

Nothing in this suite signs an order, reads a broker secret or writes the IBOR.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell"
SRC_ROOT = UI_ROOT / "src"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.orchestrator import NIOSCoreOrchestrator  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

USERNAME = "cleanroom"
PASSWORD = "masterpiece-9"
LIVE_PIN = "135790"

PAGE_KEYS = (
    "dashboard",
    "terminal",
    "orders",
    "options",
    "analyzer",
    "quant",
    "agents",
    "backtest",
    "brain",
    "forge",
    "ledger",
    "logbook",
    "verification",
    "brokers",
    "control",
)

PAGE_COMPONENTS = {
    "dashboard": "DashboardPage",
    "terminal": "TerminalPage",
    "orders": "OrdersPage",
    "options": "OptionsPage",
    "analyzer": "AnalyzerPage",
    "quant": "QuantPage",
    "agents": "AgentsPage",
    "backtest": "BacktestPage",
    "brain": "BrainPage",
    "forge": "ForgePage",
    "ledger": "LedgerPage",
    "logbook": "LogbookPage",
    "verification": "VerificationPage",
    "brokers": "BrokersPage",
    "control": "ControlPage",
}

PORTAL_ROUTES = (
    "/setup",
    "/login",
    "/paper/login",
    "/live/login",
    "/live-unlock",
)

PAGE_ENDPOINTS = (
    "pages",
    "terminal",
    "orders",
    "options",
    "analyzer",
    "quant",
    "agents",
    "backtest",
    "forge",
    "ledger",
    "logbook",
    "brokers",
    "mcp",
)


def _routes() -> list[str]:
    routes: list[str] = []
    for key in PAGE_KEYS:
        routes.append(f"/{key}")
        routes.append(f"/paper/{key}")
        routes.append(f"/live/{key}")
    routes.extend(PORTAL_ROUTES)
    return routes


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("nios-v14-ws")
    vault = tmp_path_factory.mktemp("nios-v14-vault")
    instance = NIOSCoreOrchestrator(
        workspace=workspace,
        key_vault_root=vault,
        clean_slate=True,
    )
    try:
        yield instance
    finally:
        try:
            instance.close()
        except Exception:
            pass


@pytest.fixture(scope="module")
def client(runtime):
    from ring3_cockpit.gateway.control_plane import create_app

    app = create_app(runtime.control)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def operator(client) -> None:
    response = client.post(
        "/api/v2/auth/setup",
        json={"username": USERNAME, "password": PASSWORD, "live_pin": LIVE_PIN},
    )
    assert response.status_code == 200, response.text
    assert response.json()["tier"] == "PAPER"


def _headers(client) -> dict[str, str]:
    token = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    ).json()["token"]
    return {"X-NIOS-Session": token}


# ---------------------------------------------------------------------------
# Gate 1 - clean-room architecture
# ---------------------------------------------------------------------------


def test_v1_4_clean_room_source_layout() -> None:
    required = [
        "index.css",
        "main.tsx",
        "App.tsx",
        "design-system/tokens.css",
        "design-system/primitives.tsx",
        "design-system/ring-topology.tsx",
        "charts/CandlestickChart.tsx",
        "charts/DepthLadderChart.tsx",
        "charts/PayoffCurveChart.tsx",
        "charts/EquityDrawdownChart.tsx",
        "charts/ContagionFlowGraph.tsx",
        "charts/RadialGauge.tsx",
        "charts/kit.tsx",
        "shells/PaperShell.tsx",
        "shells/LiveShell.tsx",
        "shells/Sidebar.tsx",
        "portals/SetupPortal.tsx",
        "portals/GatewayPortal.tsx",
        "portals/PaperLoginPortal.tsx",
        "portals/LiveLoginPortal.tsx",
        "portals/LiveUnlockModal.tsx",
        "lib/api.ts",
        "lib/router.ts",
        "lib/format.ts",
        "lib/types.ts",
        "lib/session.tsx",
        "lib/use-api.ts",
        "lib/toast.tsx",
        "pages/registry.ts",
    ]
    required.extend(f"pages/{PAGE_COMPONENTS[key]}.tsx" for key in PAGE_KEYS)

    missing = [relative for relative in required if not (SRC_ROOT / relative).exists()]
    assert not missing, missing

    legacy = [
        "types.ts",
        "components/app-sidebar.tsx",
        "components/page-shell.tsx",
        "pages/workspace.tsx",
        "pages/auth-views.tsx",
        "hooks/use-cockpit.ts",
        "lib/rows.ts",
    ]
    survivors = [relative for relative in legacy if (SRC_ROOT / relative).exists()]
    assert not survivors, survivors

    tokens = (SRC_ROOT / "design-system" / "tokens.css").read_text(encoding="utf-8")
    for marker in (
        "--obsidian",
        "#0d0e10",
        "#27292e",
        "font-variant-numeric: tabular-nums",
        ".nios-shell-paper",
        ".nios-shell-live",
        ".nios-hazard",
    ):
        assert marker in tokens, marker


# ---------------------------------------------------------------------------
# Gate 2 - every route serves the clean-room bundle
# ---------------------------------------------------------------------------


def test_v1_4_fifteen_pages_and_portals_serve_html(client) -> None:
    index_html = UI_ROOT / "dist" / "index.html"
    assert index_html.exists(), "run `npm run build` in ring3_cockpit/ui/openalgo_shell"
    assert "assets/" in index_html.read_text(encoding="utf-8")

    routes = _routes()
    assert len(routes) == 50
    for route in routes:
        response = client.get(route)
        assert response.status_code == 200, f"{route}: {response.text[:200]}"
        assert "text/html" in response.headers.get("content-type", ""), route
        assert "<div id=\"root\">" in response.text, route


# ---------------------------------------------------------------------------
# Gate 3 - dual-shell separation
# ---------------------------------------------------------------------------


def test_v1_4_dual_shells_are_strictly_separated() -> None:
    paper = (SRC_ROOT / "shells" / "PaperShell.tsx").read_text(encoding="utf-8")
    live = (SRC_ROOT / "shells" / "LiveShell.tsx").read_text(encoding="utf-8")

    assert "PAPER SANDBOX" in paper
    assert "Deposit Paper Capital" in paper
    assert "Reset to Zero" in paper
    assert "Autopilot" in paper
    assert "Switch to Live Vault" in paper

    assert "LIVE PRODUCTION — REAL CAPITAL" in live
    assert "EMERGENCY KILL SWITCH" in live
    assert "Lock Live Vault" in live
    assert "22-gate" in live.lower()
    assert "EMERGENCY KILL SWITCH".lower() in live.lower()
    assert "expiresAtMs" in live or "LEASE" in live

    live_lower = live.lower()
    for forbidden in (
        "deposit paper capital",
        "reset all values to zero",
        "shadow-burst",
        "shadow fills burst",
        "autopilot play",
    ):
        assert forbidden not in live_lower, forbidden

    # The paper shell must not render live-only armament.
    assert "EMERGENCY KILL SWITCH" not in paper


def test_v1_4_live_login_door_is_three_factor() -> None:
    live_login = (SRC_ROOT / "portals" / "LiveLoginPortal.tsx").read_text(
        encoding="utf-8"
    )
    assert "live_pin" in live_login
    assert "loginLive" in live_login
    assert "acknowledged" in live_login
    assert "REAL CAPITAL" in live_login.upper()
    assert "22" in live_login
    assert "password" in live_login.lower()

    setup = (SRC_ROOT / "portals" / "SetupPortal.tsx").read_text(encoding="utf-8")
    assert "live_pin" in setup
    assert "CLEAN_SLATE" in setup
    assert "All Vaults = 0" in setup

    gateway = (SRC_ROOT / "portals" / "GatewayPortal.tsx").read_text(encoding="utf-8")
    assert "ENTER PAPER SANDBOX" in gateway
    assert "LIVE SOVEREIGN VAULT" in gateway


# ---------------------------------------------------------------------------
# Gate 4 - navigation, catalog and bespoke pages
# ---------------------------------------------------------------------------


def test_v1_4_router_catalog_sidebar_and_palette_cover_every_route() -> None:
    registry = (SRC_ROOT / "pages" / "registry.ts").read_text(encoding="utf-8")
    for key in PAGE_KEYS:
        assert f'key: "{key}"' in registry, key
    for label in (
        "Core Execution",
        "Quant & Research",
        "Bicameral Brain",
        "Ledger & Audit",
        "Sovereign Control",
    ):
        assert label in registry, label
    assert registry.count("route: ") >= 15

    router = (SRC_ROOT / "lib" / "router.ts").read_text(encoding="utf-8")
    for route in PORTAL_ROUTES:
        assert route in router, route
    assert "popstate" in router
    assert "pagePath" in router

    sidebar = (SRC_ROOT / "shells" / "Sidebar.tsx").read_text(encoding="utf-8")
    assert "NAV_GROUPS" in sidebar
    assert "aria-current" in sidebar
    assert "aria-expanded" in sidebar

    palette = (SRC_ROOT / "design-system" / "primitives.tsx").read_text(
        encoding="utf-8"
    )
    assert "CommandPalette" in palette
    assert "metaKey" in palette and "ctrlKey" in palette
    assert "15 production pages" in palette

    app = (SRC_ROOT / "App.tsx").read_text(encoding="utf-8")
    for component in PAGE_COMPONENTS.values():
        assert component in app, component
    assert "PaperShell" in app and "LiveShell" in app


def test_v1_4_pages_are_bespoke_workstations() -> None:
    for key in PAGE_KEYS:
        source = (SRC_ROOT / "pages" / f"{PAGE_COMPONENTS[key]}.tsx").read_text(
            encoding="utf-8"
        )
        assert "SectionHeader" in source, key
        assert "PageShell" not in source, key
        panels = sum(
            marker in source
            for marker in ("TerminalPanel", "MetricTile", "StatusBadge", "DeltaBadge")
        )
        assert panels >= 2, key
        assert "export function" in source, key


# ---------------------------------------------------------------------------
# Gate 5 - clean slate renders zero, not null
# ---------------------------------------------------------------------------


def test_v1_4_clean_slate_zero_values_render(client, operator) -> None:
    headers = _headers(client)

    paper = client.get("/api/v2/workspace/paper/state", headers=headers)
    assert paper.status_code == 200, paper.text
    body = paper.json()
    assert body["clean_slate_active"] is True
    assert body["orders"]["working"] == []
    assert body["shadow"]["shadow_fills"] == 0
    assert body["shadow"]["required_fills"] == 30
    assert body["shadow"]["graduated"] is False
    assert body["autopilot"]["enabled"] is False
    for silo in body["silos"].values():
        assert silo["nav_micros"] == 0

    ledger = client.get("/api/v2/cockpit/ledger", headers=headers)
    assert ledger.status_code == 200, ledger.text
    journal = ledger.json()["double_entry"]
    assert journal["debits_micros"] == 0
    assert journal["credits_micros"] == 0
    assert journal["balanced"] is True

    logbook = client.get("/api/v2/cockpit/logbook", headers=headers)
    assert logbook.status_code == 200, logbook.text
    assert logbook.json()["entries"] == []

    analyzer = client.get("/api/v2/cockpit/analyzer", headers=headers)
    assert analyzer.status_code == 200, analyzer.text
    nursery = analyzer.json()["nursery"]
    assert nursery["required_fills"] == 30
    assert nursery["graduated_count"] == 0
    for row in nursery["strategies"]:
        assert row["completed_fills"] == 0
        assert row["graduated"] is False

    for endpoint in PAGE_ENDPOINTS:
        response = client.get(f"/api/v2/cockpit/{endpoint}", headers=headers)
        assert response.status_code == 200, f"{endpoint}: {response.text[:300]}"


# ---------------------------------------------------------------------------
# Gate 6 - lint + bundle markers
# ---------------------------------------------------------------------------


def test_v1_4_biome_is_clean_and_bundle_carries_every_marker() -> None:
    npx = shutil.which("npx")
    if npx is None:  # pragma: no cover - node toolchain is optional
        pytest.skip("npx is not available on this host")

    completed = subprocess.run(
        [npx, "biome", "lint", "src/"],
        cwd=str(UI_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    combined = f"{completed.stdout}{completed.stderr}"
    assert completed.returncode == 0, combined[-4000:]
    lowered = combined.lower()
    assert "found 0 errors" in lowered or "checked" in lowered

    assets = list((UI_ROOT / "dist" / "assets").glob("*.js"))
    assert assets, "dist/assets has no JavaScript bundle"
    joined = "".join(path.read_text(encoding="utf-8") for path in assets).lower()
    for marker in ("paper sandbox", "live production", "clean slate", "15 production pages"):
        assert marker in joined, marker
