"""V1.5 - clean-room institutional financial terminal UI.

Gate inventory for the flat-terminal release (``v3.6.0-V1.5``):

1. The V1.5 source tree exists exactly where the blueprint says it does, and the
   withdrawn V1.4 illustration module is gone.
2. The compiled ``dist/`` bundle carries all twelve institutional colour tokens
   and the global ``tabular-nums`` rule.
3. There is zero glassmorphism, zero drop shadow, zero neon bloom, zero gradient
   fill and zero pill radius anywhere in ``src/`` or ``dist/``.
4. All fifty routes - fifteen pages x three workspaces plus five portals - answer
   ``200 text/html`` from the built bundle.
5. Paper and live shells stay physically separated; the live surface renders the
   hazard ribbon, the 22-gate chip, the 30-minute lease and the two-step kill
   switch and never a deposit, a reset or an autopilot transport.
6. The six flat SVG charts render without a gradient primitive.
7. Radius discipline: every module radius sits between 4px and 8px.

Nothing in this suite signs an order, reads a broker secret or writes the IBOR.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell"
SRC_ROOT = UI_ROOT / "src"
DIST_ROOT = UI_ROOT / "dist"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.orchestrator import NIOSCoreOrchestrator  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

USERNAME = "terminal"
PASSWORD = "flat-terminal-9"
LIVE_PIN = "246810"

TOKENS = {
    "canvas": "#0d0e10",
    "surface": "#16181c",
    "raised": "#22242a",
    "hairline": "#27292e",
    "accent": "#f96316",
    "text": "#f4f5f7",
    "text_secondary": "#8c9099",
    "text_muted": "#5a5e66",
    "bull": "#38b27b",
    "bull_tint": "#172b23",
    "bear": "#e05555",
    "bear_tint": "#2d191a",
}

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

CHARTS = (
    "CandlestickChart.tsx",
    "DepthLadderChart.tsx",
    "PayoffCurveChart.tsx",
    "EquityDrawdownChart.tsx",
    "ContagionFlowGraph.tsx",
    "RadialGauge.tsx",
)

PORTAL_ROUTES = (
    "/setup",
    "/login",
    "/paper/login",
    "/live/login",
    "/live-unlock",
)

FORBIDDEN = (
    "backdrop-filter",
    "backdrop-blur",
    "box-shadow",
    "lineargradient",
    "radialgradient",
    "rounded-full",
    "rounded-xl",
    "rounded-2xl",
    "9999px",
)


def _routes() -> list[str]:
    routes: list[str] = []
    for key in PAGE_KEYS:
        routes.append(f"/{key}")
        routes.append(f"/paper/{key}")
        routes.append(f"/live/{key}")
    routes.extend(PORTAL_ROUTES)
    return routes


def _source_files() -> list[Path]:
    return [path for path in SRC_ROOT.rglob("*") if path.is_file()]


def _bundle_text() -> str:
    css = "\n".join(path.read_text(encoding="utf-8") for path in DIST_ROOT.glob("assets/*.css"))
    js = "\n".join(path.read_text(encoding="utf-8") for path in DIST_ROOT.glob("assets/*.js"))
    html = (DIST_ROOT / "index.html").read_text(encoding="utf-8")
    return f"{css}\n{js}\n{html}"


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("nios-v15-ws")
    vault = tmp_path_factory.mktemp("nios-v15-vault")
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


# ---------------------------------------------------------------------------
# Gate 1 - clean-room architecture
# ---------------------------------------------------------------------------


def test_v1_5_flat_terminal_source_layout() -> None:
    required = [
        "index.css",
        "main.tsx",
        "App.tsx",
        "design-system/tokens.css",
        "design-system/primitives.tsx",
        "design-system/ring-topology.tsx",
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
        "lib/session.tsx",
        "lib/toast.tsx",
        "lib/types.ts",
        "lib/use-api.ts",
        "pages/registry.ts",
    ]
    required.extend(f"charts/{name}" for name in CHARTS)
    required.extend(f"pages/{PAGE_COMPONENTS[key]}.tsx" for key in PAGE_KEYS)

    missing = [relative for relative in required if not (SRC_ROOT / relative).exists()]
    assert not missing, missing

    withdrawn = [
        "design-system/isometric-rings.tsx",
        "index.css.bak",
        "components/page-shell.tsx",
    ]
    survivors = [relative for relative in withdrawn if (SRC_ROOT / relative).exists()]
    assert not survivors, survivors


def test_v1_5_tokens_are_the_institutional_palette() -> None:
    tokens = (SRC_ROOT / "design-system" / "tokens.css").read_text(encoding="utf-8")
    for name, value in TOKENS.items():
        assert value in tokens, name
    assert "font-variant-numeric: tabular-nums" in tokens
    assert "--nx-r-1: 4px" in tokens
    assert "--nx-r-2: 6px" in tokens
    assert "--nx-r-3: 8px" in tokens
    assert "tabular-nums" in (SRC_ROOT / "index.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Gate 2 - flat surfaces only
# ---------------------------------------------------------------------------


def test_v1_5_zero_glass_zero_shadow_zero_pill() -> None:
    for path in _source_files():
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for forbidden in FORBIDDEN:
            assert forbidden not in text, f"{forbidden} found in {path.name}"
    bundle = _bundle_text().lower()
    for forbidden in FORBIDDEN:
        assert forbidden not in bundle, forbidden


def test_v1_5_charts_are_flat_and_gradient_free() -> None:
    for name in CHARTS:
        source = (SRC_ROOT / "charts" / name).read_text(encoding="utf-8")
        assert "linearGradient" not in source, name
        assert "radialGradient" not in source, name
        assert "strokeWidth" in source, name

    kit = (SRC_ROOT / "charts" / "kit.tsx").read_text(encoding="utf-8")
    for key in ("grid", "accent", "bull", "bear"):
        assert key in kit, key
    assert "#27292E" in kit


def test_v1_5_radius_scale_is_tight() -> None:
    css = (SRC_ROOT / "index.css").read_text(encoding="utf-8")
    radii = {int(value) for value in re.findall(r"border-radius:\s*(\d+)px", css)}
    assert radii, "no numeric border radius declared"
    assert max(radii) <= 8, radii
    assert min(radii) >= 1, radii
    tokens = (SRC_ROOT / "design-system" / "tokens.css").read_text(encoding="utf-8")
    token_radii = {int(value) for value in re.findall(r"--nx-r-\d:\s*(\d+)px", tokens)}
    assert token_radii == {4, 6, 8}, token_radii


# ---------------------------------------------------------------------------
# Gate 3 - the compiled bundle
# ---------------------------------------------------------------------------


def test_v1_5_bundle_carries_every_token_and_tabular_nums() -> None:
    bundle = _bundle_text().lower()
    for name, value in TOKENS.items():
        assert value in bundle, name
    assert "tabular-nums" in bundle
    assert "assets/" in (DIST_ROOT / "index.html").read_text(encoding="utf-8")

    for marker in ("paper sandbox", "live production", "clean slate", "15 production pages"):
        assert marker in bundle, marker


# ---------------------------------------------------------------------------
# Gate 4 - every route serves the flat bundle
# ---------------------------------------------------------------------------


def test_v1_5_fifty_routes_serve_html(client, operator) -> None:
    routes = _routes()
    assert len(routes) == 50
    for route in routes:
        response = client.get(route)
        assert response.status_code == 200, f"{route}: {response.text[:200]}"
        assert "text/html" in response.headers.get("content-type", ""), route
        assert '<div id="root">' in response.text, route


# ---------------------------------------------------------------------------
# Gate 5 - shell separation and terminal primitives
# ---------------------------------------------------------------------------


def test_v1_5_dual_shells_are_strictly_separated() -> None:
    paper = (SRC_ROOT / "shells" / "PaperShell.tsx").read_text(encoding="utf-8")
    live = (SRC_ROOT / "shells" / "LiveShell.tsx").read_text(encoding="utf-8")

    assert "PAPER SANDBOX" in paper
    assert "Deposit Paper Capital" in paper
    assert "Reset to Zero" in paper
    assert "Autopilot" in paper
    assert "Switch to Live Vault" in paper
    assert "EMERGENCY KILL SWITCH" not in paper

    assert "LIVE PRODUCTION" in live
    assert "REAL CAPITAL" in live
    assert "EMERGENCY KILL SWITCH" in live
    assert "Lock Live Vault" in live
    assert "22-gate" in live.lower()
    assert "expiresAtMs" in live

    lowered = live.lower()
    for forbidden in (
        "deposit paper capital",
        "reset to zero",
        "shadow-burst",
        "shadow fills burst",
        "autopilot play",
        "pause autopilot",
    ):
        assert forbidden not in lowered, forbidden


def test_v1_5_portals_guard_the_three_factor_door() -> None:
    live_login = (SRC_ROOT / "portals" / "LiveLoginPortal.tsx").read_text(encoding="utf-8")
    assert "live_pin" in live_login
    assert "loginLive" in live_login
    assert "acknowledged" in live_login
    assert "REAL CAPITAL" in live_login.upper()
    assert "22" in live_login
    assert "password" in live_login.lower()

    paper_login = (SRC_ROOT / "portals" / "PaperLoginPortal.tsx").read_text(encoding="utf-8")
    assert "loginPaper" in paper_login
    assert "live_pin" not in paper_login
    assert "/paper/dashboard" in paper_login

    setup = (SRC_ROOT / "portals" / "SetupPortal.tsx").read_text(encoding="utf-8")
    assert "live_pin" in setup
    assert "CLEAN_SLATE" in setup
    assert "All Vaults = 0" in setup

    gateway = (SRC_ROOT / "portals" / "GatewayPortal.tsx").read_text(encoding="utf-8")
    assert "PAPER SANDBOX" in gateway
    assert "LIVE SOVEREIGN VAULT" in gateway


def test_v1_5_pages_are_bespoke_workstations() -> None:
    for key in PAGE_KEYS:
        source = (SRC_ROOT / "pages" / f"{PAGE_COMPONENTS[key]}.tsx").read_text(encoding="utf-8")
        assert "SectionHeader" in source, key
        assert "PageShell" not in source, key
        panels = sum(
            marker in source
            for marker in ("TerminalPanel", "MetricTile", "StatusBadge", "DeltaBadge")
        )
        assert panels >= 2, key
        assert "export function" in source, key


def test_v1_5_router_catalog_and_palette_cover_every_route() -> None:
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

    router = (SRC_ROOT / "lib" / "router.ts").read_text(encoding="utf-8")
    for route in PORTAL_ROUTES:
        assert route in router, route
    assert "popstate" in router
    assert "pagePath" in router

    sidebar = (SRC_ROOT / "shells" / "Sidebar.tsx").read_text(encoding="utf-8")
    assert "NAV_GROUPS" in sidebar
    assert "aria-current" in sidebar
    assert "aria-expanded" in sidebar

    primitives = (SRC_ROOT / "design-system" / "primitives.tsx").read_text(encoding="utf-8")
    assert "CommandPalette" in primitives
    assert "metaKey" in primitives and "ctrlKey" in primitives
    assert "15 production pages" in primitives
    assert "TerminalPanel" in primitives
    assert "ConfirmModal" in primitives

    app = (SRC_ROOT / "App.tsx").read_text(encoding="utf-8")
    for component in PAGE_COMPONENTS.values():
        assert component in app, component
    assert "PaperShell" in app and "LiveShell" in app
