import { type ComponentType, useEffect } from "react";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { StatusDot } from "./components/ui";
import { WORKSTATION_KEYS, WORKSTATIONS } from "./config/nav";
import { RouterProvider, usePathname, useRouter } from "./lib/router";
import AgentsPage from "./pages/AgentsPage";
import AnalyzerPage from "./pages/AnalyzerPage";
import BacktestPage from "./pages/BacktestPage";
import BrainPage from "./pages/BrainPage";
import BrokersPage from "./pages/BrokersPage";
import ControlPage from "./pages/ControlPage";
import DashboardPage from "./pages/DashboardPage";
import ForgePage from "./pages/ForgePage";
import LedgerPage from "./pages/LedgerPage";
import LogbookPage from "./pages/LogbookPage";
import MissionsPage from "./pages/MissionsPage";
import OptionsPage from "./pages/OptionsPage";
import OrdersPage from "./pages/OrdersPage";
import QuantPage from "./pages/QuantPage";
import ScreenerPage from "./pages/ScreenerPage";
import TerminalPage from "./pages/TerminalPage";
import VerificationPage from "./pages/VerificationPage";
import type { PageProps } from "./pages/types";
import { GatewayPortal } from "./portals/GatewayPortal";
import { LiveLoginPortal } from "./portals/LiveLoginPortal";
import { LiveUnlockModal } from "./portals/LiveUnlockModal";
import { PaperLoginPortal } from "./portals/PaperLoginPortal";
import { SetupPortal } from "./portals/SetupPortal";
import { LiveShell } from "./shells/LiveShell";
import { PaperShell } from "./shells/PaperShell";
import { SessionProvider, useSession } from "./state/session";

const PAGES: Record<string, ComponentType<PageProps>> = {
  dashboard: DashboardPage,
  missions: MissionsPage,
  screener: ScreenerPage,
  terminal: TerminalPage,
  orders: OrdersPage,
  options: OptionsPage,
  analyzer: AnalyzerPage,
  quant: QuantPage,
  agents: AgentsPage,
  backtest: BacktestPage,
  brain: BrainPage,
  forge: ForgePage,
  ledger: LedgerPage,
  logbook: LogbookPage,
  verification: VerificationPage,
  brokers: BrokersPage,
  control: ControlPage,
};

function Home() {
  const { session, loading } = useSession();
  const { navigate } = useRouter();
  useEffect(() => {
    if (loading) return;
    if (!session?.authenticated) navigate("/login", { replace: true });
    else if (session.mode === "LIVE") navigate("/live/dashboard", { replace: true });
    else navigate("/paper/dashboard", { replace: true });
  }, [loading, navigate, session]);
  return (
    <div className="flex h-screen items-center justify-center bg-canvas text-[10px] uppercase tracking-[0.18em] text-ink-3">
      <StatusDot tone="accent" pulse /> RESOLVING SESSIONâ€¦
    </div>
  );
}

function NotFound({ path }: { path: string }) {
  return (
    <div className="flex h-screen flex-col items-center justify-center gap-3 bg-canvas text-ink">
      <div className="text-[11px] font-semibold uppercase tracking-[0.2em] text-ink-3">404 Â· NO SUCH WORKSTATION</div>
      <div className="font-mono text-[11px] text-ink-3">{path}</div>
      <a href="/paper/dashboard" className="rounded-[4px] border border-hairline bg-elevated px-2.5 py-1 text-[10px] uppercase tracking-[0.12em] text-ink-2 hover:text-ink">
        Return to dashboard
      </a>
    </div>
  );
}

function Routes() {
  const pathname = usePathname();

  if (pathname === "/" || pathname === "") return <Home />;
  if (pathname === "/setup") return <SetupPortal />;
  if (pathname === "/login") return <GatewayPortal />;
  if (pathname === "/paper/login") return <PaperLoginPortal />;
  if (pathname === "/live/login") return <LiveLoginPortal />;
  if (pathname === "/live-unlock") return <LiveUnlockModal />;

  const segments = pathname.split("/").filter((segment) => segment.length > 0);

  if (segments[0] === "paper" || segments[0] === "live") {
    const workspace = segments[0];
    const key = segments[1] ?? "dashboard";
    if (!WORKSTATION_KEYS.includes(key)) return <NotFound path={pathname} />;
    const Page = PAGES[key];
    const mode = workspace === "live" ? "LIVE" : "PAPER";
    return (
      <ErrorBoundary key={`${workspace}:${key}`}>
        {workspace === "live" ? (
          <LiveShell>
            <Page mode={mode} />
          </LiveShell>
        ) : (
          <PaperShell>
            <Page mode={mode} />
          </PaperShell>
        )}
      </ErrorBoundary>
    );
  }

  if (segments.length === 1 && WORKSTATION_KEYS.includes(segments[0])) {
    const key = segments[0];
    const Page = PAGES[key];
    return (
      <ErrorBoundary key={`paper:${key}`}>
        <PaperShell>
          <Page mode="PAPER" />
        </PaperShell>
      </ErrorBoundary>
    );
  }

  return <NotFound path={pathname} />;
}

export function App() {
  return (
    <RouterProvider>
      <SessionProvider>
        <Routes />
      </SessionProvider>
    </RouterProvider>
  );
}

export const ROUTE_CENSUS = {
  portals: ["/setup", "/login", "/paper/login", "/live/login", "/live-unlock"],
  workstations: WORKSTATIONS.map((station) => station.key),
  base: WORKSTATIONS.map((station) => `/${station.key}`),
  paper: WORKSTATIONS.map((station) => `/paper/${station.key}`),
  live: WORKSTATIONS.map((station) => `/live/${station.key}`),
};
