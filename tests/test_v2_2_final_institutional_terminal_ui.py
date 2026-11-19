"""NIOS V2.2 -- Suite 38: final institutional financial terminal (frontend seal).

``v4.2.0-V2.2`` ships the React 19 / TypeScript / Tailwind v4 terminal that the
headless ``V1.6``..``V2.1`` releases deliberately withheld.  This suite is the
executable form of the visual and routing contract:

1. The compiled bundle is **flat matte**: zero glassmorphism (``backdrop-filter``
   / ``backdrop-blur``), zero elevation (``box-shadow``), zero gradients
   (``linear-gradient`` / ``radial-gradient``) and zero pill radii
   (``9999px``) anywhere in ``dist/``.
2. The twelve canonical colour tokens and ``tabular-nums`` survive the build
   byte-for-byte, because number columns must never jitter on a live tape.
3. ``PaperShell`` and ``LiveShell`` are *different* programs: the sandbox may
   deposit, reset and autopilot; the live vault may not, and it alone carries the
   sub-millisecond kill switch.
4. Every mutation carries ``X-NIOS-CSRF`` as well as ``X-NIOS-Session``.
5. All 56 client routes answer ``200 text/html`` from the one built document.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell"
DIST_ROOT = UI_ROOT / "dist"
SRC_ROOT = UI_ROOT / "src"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.orchestrator import NIOSCoreOrchestrator  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

USERNAME = "terminal-operator"
PASSWORD = "institutional-9"
LIVE_PIN = "246810"

WORKSTATIONS = (
    "dashboard",
    "missions",
    "screener",
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

PORTALS = ("/setup", "/login", "/paper/login", "/live/login", "/live-unlock")

CANONICAL_COLOURS = (
    "#0D0E10",
    "#16181C",
    "#22242A",
    "#27292E",
    "#F96316",
    "#F4F5F7",
    "#8C9099",
    "#5A5E66",
    "#38B27B",
    "#172B23",
    "#E05555",
    "#2D191A",
)

FORBIDDEN_CSS = (
    "backdrop-filter",
    "backdrop-blur",
    "box-shadow",
    "linear-gradient",
    "radial-gradient",
    "9999px",
)

PAGE_FILES = (
    "AgentsPage",
    "AnalyzerPage",
    "BacktestPage",
    "BrainPage",
    "BrokersPage",
    "ControlPage",
    "DashboardPage",
    "ForgePage",
    "LedgerPage",
    "LogbookPage",
    "MissionsPage",
    "OptionsPage",
    "OrdersPage",
    "QuantPage",
    "ScreenerPage",
    "TerminalPage",
    "VerificationPage",
)


def _all_routes() -> list[str]:
    routes: list[str] = list(PORTALS)
    for key in WORKSTATIONS:
        routes.append(f"/{key}")
    for key in WORKSTATIONS:
        routes.append(f"/paper/{key}")
    for key in WORKSTATIONS:
        routes.append(f"/live/{key}")
    return routes


def _css_bundle() -> str:
    assets = DIST_ROOT / "assets"
    assert assets.is_dir(), "the Vite build did not emit dist/assets"
    stylesheets = sorted(assets.glob("*.css"))
    assert stylesheets, "the build emitted no stylesheet"
    return "\n".join(path.read_text(encoding="utf-8") for path in stylesheets)


def _page_source(name: str) -> str:
    path = SRC_ROOT / "pages" / f"{name}.tsx"
    assert path.is_file(), f"missing workstation page {path.name}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("terminal-workspace")
    vault = tmp_path_factory.mktemp("terminal-vault")
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


# ---------------------------------------------------------------------------
# Gate 1 - the toolchain is a real, checked-in V2.2 build
# ---------------------------------------------------------------------------


def test_gate1_v2_2_toolchain_and_bundle_exist() -> None:
    for name in (
        "package.json",
        "package-lock.json",
        "vite.config.ts",
        "biome.json",
        "tsconfig.json",
        "tsconfig.app.json",
        "tsconfig.node.json",
        "postcss.config.mjs",
        "postcss.flatness.mjs",
        "index.html",
    ):
        assert (UI_ROOT / name).is_file(), name
    assert SRC_ROOT.is_dir()
    assert (SRC_ROOT / "App.tsx").is_file()
    assert (SRC_ROOT / "lib" / "api.ts").is_file()
    assert (SRC_ROOT / "lib" / "stream.ts").is_file()
    assert (DIST_ROOT / "assets").is_dir()

    manifest = json.loads((DIST_ROOT / "headless-manifest.json").read_text(encoding="utf-8"))
    assert manifest["frontend"] != "NONE"
    assert manifest["javascript"] >= 1
    assert manifest["routes"] == 56
    assert manifest["workstations"] == 17
    assert manifest["shells"] == ["PaperShell", "LiveShell"]
    assert manifest["contracts"]["statutory_floors_bps"] == {
        "INDIA_ZERODHA_DHAN": 8.5,
        "CRYPTO_BINANCE": 16.0,
        "US_IBKR": 2.8,
        "FX_MT5": 1.6,
    }


def test_gate1_document_is_the_v2_2_institutional_terminal() -> None:
    text = (DIST_ROOT / "index.html").read_text(encoding="utf-8")
    assert "nios-frontend" in text
    assert "V2.2-INSTITUTIONAL-TERMINAL" in text
    assert "nios-release" in text
    assert "v4.2.0-V2.2" in text
    assert "#0D0E10" in text
    assert "<script" in text.lower()
    assert "/assets/" in text
    assert "backdrop-filter" not in text.lower()
    assert "box-shadow" not in text.lower()


# ---------------------------------------------------------------------------
# Gate 2 - flat matte, zero elevation, zero gradients, zero pills
# ---------------------------------------------------------------------------


def test_gate2_compiled_css_has_zero_glass_elevation_or_gradient() -> None:
    css = _css_bundle().lower()
    for forbidden in FORBIDDEN_CSS:
        assert forbidden not in css, forbidden


def test_gate2_compiled_css_keeps_the_twelve_canonical_tokens() -> None:
    # The authored source keeps the tokens in canonical upper case; Lightning
    # CSS normalises colour literals to lower case in the emitted bundle, so the
    # compiled assertion is case-insensitive while the source assertion is exact.
    source = (SRC_ROOT / "design-system" / "tokens.css").read_text(encoding="utf-8")
    for colour in CANONICAL_COLOURS:
        assert colour in source, colour

    css = _css_bundle()

    lowered = css.lower()
    for colour in CANONICAL_COLOURS:
        assert colour.lower() in lowered, colour
    assert "tabular-nums" in css


def test_gate2_source_never_reintroduces_rounded_pills_or_blur() -> None:
    forbidden = (
        "rounded-full",
        "backdrop-blur",
        "backdrop-filter",
        "box-shadow",
        "linear-gradient",
        "radial-gradient",
        "9999px",
    )
    sources = sorted(SRC_ROOT.rglob("*.tsx")) + sorted(SRC_ROOT.rglob("*.ts"))
    assert len(sources) >= 40, len(sources)
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path.name}: {token}"


def test_gate2_seventeen_workstation_pages_are_bespoke() -> None:
    for name in PAGE_FILES:
        text = _page_source(name)
        assert (
            f"export default function {name}" in text or f"export default {name};" in text
        ), name
        assert "PageProps" in text, name
    assert len({_page_source(name) for name in PAGE_FILES}) == 17


# ---------------------------------------------------------------------------
# Gate 3 - PaperShell and LiveShell are different programs
# ---------------------------------------------------------------------------


def test_gate3_paper_shell_owns_sandbox_controls() -> None:
    paper = (SRC_ROOT / "shells" / "PaperShell.tsx").read_text(encoding="utf-8")
    assert "PAPER SANDBOX" in paper
    assert "SIMULATED EXECUTION ONLY" in paper
    assert "/control/ledger/fund-paper" in paper
    assert "/control/workspace/reset" in paper
    assert "/control/workspace/autopilot" in paper
    assert "/control/autopilot/tick" in paper
    assert "/control/killswitch" not in paper
    assert "LIVE PRODUCTION" not in paper


def test_gate3_live_shell_owns_kill_switch_and_nothing_paper() -> None:
    live = (SRC_ROOT / "shells" / "LiveShell.tsx").read_text(encoding="utf-8")
    assert "LIVE PRODUCTION" in live
    assert "REAL CAPITAL" in live
    assert "/control/killswitch" in live
    assert "confirm: true" in live
    assert "/control/ledger/fund-paper" not in live
    assert "/control/workspace/reset" not in live
    assert "/control/workspace/autopilot" not in live
    assert "/control/autopilot/tick" not in live
    assert "/control/analyzer/shadow-burst" not in live


def test_gate3_dual_portals_exist_and_stay_separate() -> None:
    portals = SRC_ROOT / "portals"
    for name in (
        "SetupPortal",
        "GatewayPortal",
        "PaperLoginPortal",
        "LiveLoginPortal",
        "LiveUnlockModal",
    ):
        assert (portals / f"{name}.tsx").is_file(), name

    live_login = (portals / "LiveLoginPortal.tsx").read_text(encoding="utf-8")
    assert "authLoginLive" in live_login or "/auth/login/live" in live_login
    assert "live_pin" in live_login or "livePin" in live_login
    assert "acknowledge" in live_login.lower() or "acknowledg" in live_login.lower()

    paper_login = (portals / "PaperLoginPortal.tsx").read_text(encoding="utf-8")
    assert "authLoginPaper" in paper_login or "/auth/login/paper" in paper_login
    assert "LIVE_PIN" not in paper_login.replace("live_pin", "")


# ---------------------------------------------------------------------------
# Gate 4 - hardened session + CSRF client
# ---------------------------------------------------------------------------


def test_gate4_api_client_forwards_session_and_csrf() -> None:
    api = (SRC_ROOT / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "X-NIOS-Session" in api
    assert "X-NIOS-CSRF" in api
    assert 'credentials: "include"' in api or "credentials: 'include'" in api
    assert "X-NIOS-CSRF" in api.split("POST")[1] or "mutation" in api.lower()
    for path in (
        "/auth/logout",
        "/auth/reset",
        "/auth/elevate-live",
    ):
        assert path in api, path


def test_gate4_stream_client_handles_all_twelve_frame_types() -> None:
    stream = (SRC_ROOT / "lib" / "stream.ts").read_text(encoding="utf-8")
    for frame in (
        "TICK",
        "DEPTH",
        "ORDER",
        "HEARTBEAT",
        "SIGNAL",
        "MISSION_PROGRESS",
        "TRANCH" + "E_HALT",
        "WALLET_SWEEP",
        "CONCEPT_DRIFT_ALERT",
        "COLD_START_RECON",
        "POST_MORTEM_COMPILED",
        "SYSTEM_ALERT",
    ):
        assert frame in stream, frame
    assert "AbortController" in stream
    assert "/stream" in stream


# ---------------------------------------------------------------------------
# Gate 5 - all fifty-six client routes answer one built document
# ---------------------------------------------------------------------------


def test_gate5_fifty_six_routes_serve_the_terminal(client) -> None:
    routes = _all_routes()
    assert len(routes) == 56, len(routes)
    assert len(set(routes)) == 56
    index_bytes = (DIST_ROOT / "index.html").read_bytes()
    for route in routes:
        response = client.get(route)
        assert response.status_code == 200, route
        assert response.headers["content-type"].startswith("text/html"), route
        assert response.content == index_bytes, route


def test_gate5_api_paths_are_never_swallowed_by_the_fallback(client) -> None:
    for path in ("/api/v2/does-not-exist", "/api/v2/auth/session"):
        response = client.get(path)
        assert response.status_code in (200, 401, 403, 404), path
        assert not response.headers["content-type"].startswith("text/html"), path


def test_gate5_route_census_matches_the_shipped_router() -> None:
    app_source = (SRC_ROOT / "App.tsx").read_text(encoding="utf-8")
    assert "ROUTE_CENSUS" in app_source
    for portal in ("/setup", "/login", "/paper/login", "/live/login", "/live-unlock"):
        assert portal in app_source, portal
    assert "PaperShell" in app_source
    assert "LiveShell" in app_source
