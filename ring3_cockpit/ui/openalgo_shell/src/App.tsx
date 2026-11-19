import {
  type LazyExoticComponent,
  type ReactNode,
  Suspense,
  lazy,
  useEffect,
} from "react";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { StatusDot } from "./components/ui";
import { WORKSTATION_KEYS, WORKSTATIONS } from "./config/nav";
import { RouterProvider, usePathname, useRouter } from "./lib/router";
import type { PageProps } from "./pages/types";
import { LiveShell } from "./shells/LiveShell";
import { PaperShell } from "./shells/PaperShell";
import { SessionProvider, useSession } from "./state/session";

type RoutePage = LazyExoticComponent<(props: PageProps) => ReactNode>;

/**
 * Every workstation is a route-level chunk.  The shells, the router and the
 * session provider stay in the entry bundle so navigation never remounts the
 * chrome around the page.
 */
const PAGES: Record<string, RoutePage> = {
  dashboard: lazy(() => import("./pages/DashboardPage")),
  missions: lazy(() => import("./pages/MissionsPage")),
  screener: lazy(() => import("./pages/ScreenerPage")),
  terminal: lazy(() => import("./pages/TerminalPage")),
  orders: lazy(() => import("./pages/OrdersPage")),
  options: lazy(() => import("./pages/OptionsPage")),
  analyzer: lazy(() => import("./pages/AnalyzerPage")),
  quant: lazy(() => import("./pages/QuantPage")),
  agents: lazy(() => import("./pages/AgentsPage")),
  backtest: lazy(() => import("./pages/BacktestPage")),
  brain: lazy(() => import("./pages/BrainPage")),
  forge: lazy(() => import("./pages/ForgePage")),
  ledger: lazy(() => import("./pages/LedgerPage")),
  logbook: lazy(() => import("./pages/LogbookPage")),
  verification: lazy(() => import("./pages/VerificationPage")),
  brokers: lazy(() => import("./pages/BrokersPage")),
  control: lazy(() => import("./pages/ControlPage")),
};

/** Portal routes are lazy too — a signed-out operator never downloads a workstation. */
const SetupPortal = lazy(() => import("./portals/SetupPortal").then((module) => ({ default: module.SetupPortal })));
const GatewayPortal = lazy(() =>
  import("./portals/GatewayPortal").then((module) => ({ default: module.GatewayPortal })),
);
const PaperLoginPortal = lazy(() =>
  import("./portals/PaperLoginPortal").then((module) => ({ default: module.PaperLoginPortal })),
);
const LiveLoginPortal = lazy(() =>
  import("./portals/LiveLoginPortal").then((module) => ({ default: module.LiveLoginPortal })),
);
const LiveUnlockModal = lazy(() =>
  import("./portals/LiveUnlockModal").then((module) => ({ default: module.LiveUnlockModal })),
);

/**
 * Flat matte route loader.
 *
 * #0D0E10 canvas, 1px #27292E hairline, no animation, no shadow, no blur, no
 * gradient — the same surfaces as every shipped workstation.
 */
function RouteFallback({ variant }: { variant: "screen" | "page" }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className={
        variant === "screen"
          ? "flex min-h-screen items-center justify-center bg-[#0D0E10]"
          : "flex min-h-[220px] items-center justify-center"
      }
    >
      <div className="border border-[#27292E] bg-[#16181C] px-4 py-3">
        <div className="text-[10px] font-semibold uppercase tracking-[0.2em] text-ink-2">NIOS</div>
        <div className="mt-1 text-[9.5px] uppercase tracking-[0.14em] text-ink-3">LOADING WORKSTATION</div>
      </div>
    </div>
  );
}

/** One shared Suspense boundary for every lazy route. */
function RouteSuspense({ variant, children }: { variant: "screen" | "page"; children: ReactNode }) {
  return <Suspense fallback={<RouteFallback variant={variant} />}>{children}</Suspense>;
}

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
      <StatusDot tone="accent" pulse /> RESOLVING SESSION…
    </div>
  );
}

function NotFound({ path }: { path: string }) {
  return (
    <div className="flex h-screen flex-col items-center justify-center gap-3 bg-canvas text-ink">
      <div className="text-[11px] font-semibold uppercase tracking-[0.2em] text-ink-3">404 · NO SUCH WORKSTATION</div>
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
  if (pathname === "/setup")
    return (
      <RouteSuspense variant="screen">
        <SetupPortal />
      </RouteSuspense>
    );
  if (pathname === "/login")
    return (
      <RouteSuspense variant="screen">
        <GatewayPortal />
      </RouteSuspense>
    );
  if (pathname === "/paper/login")
    return (
      <RouteSuspense variant="screen">
        <PaperLoginPortal />
      </RouteSuspense>
    );
  if (pathname === "/live/login")
    return (
      <RouteSuspense variant="screen">
        <LiveLoginPortal />
      </RouteSuspense>
    );
  if (pathname === "/live-unlock")
    return (
      <RouteSuspense variant="screen">
        <LiveUnlockModal />
      </RouteSuspense>
    );

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
            <RouteSuspense variant="page">
              <Page mode={mode} />
            </RouteSuspense>
          </LiveShell>
        ) : (
          <PaperShell>
            <RouteSuspense variant="page">
              <Page mode={mode} />
            </RouteSuspense>
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
          <RouteSuspense variant="page">
            <Page mode="PAPER" />
          </RouteSuspense>
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
