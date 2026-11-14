/**
 * V1.5 application root.
 *
 * One router, two shells, five portals and fifteen workstations. The root is
 * intentionally thin: it chooses a surface, guards the tier boundary and mounts
 * the global command palette plus the toast bus. Every screen below it owns its
 * own data.
 */

import { useEffect, useMemo } from "react";
import type { ComponentType, ReactNode } from "react";
import { useLocation, navigate, pagePath, portalPath, type Portal } from "@/lib/router";
import { useSession } from "@/lib/session";
import { CommandPalette, openPalette } from "@/design-system/primitives";
import { ToastHost } from "@/lib/toast";
import { PaperShell } from "@/shells/PaperShell";
import { LiveShell } from "@/shells/LiveShell";
import { SetupPortal } from "@/portals/SetupPortal";
import { GatewayPortal } from "@/portals/GatewayPortal";
import { PaperLoginPortal } from "@/portals/PaperLoginPortal";
import { LiveLoginPortal } from "@/portals/LiveLoginPortal";
import { LiveUnlockModal } from "@/portals/LiveUnlockModal";
import { DashboardPage } from "@/pages/DashboardPage";
import { TerminalPage } from "@/pages/TerminalPage";
import { OrdersPage } from "@/pages/OrdersPage";
import { OptionsPage } from "@/pages/OptionsPage";
import { AnalyzerPage } from "@/pages/AnalyzerPage";
import { QuantPage } from "@/pages/QuantPage";
import { AgentsPage } from "@/pages/AgentsPage";
import { BacktestPage } from "@/pages/BacktestPage";
import { BrainPage } from "@/pages/BrainPage";
import { ForgePage } from "@/pages/ForgePage";
import { LedgerPage } from "@/pages/LedgerPage";
import { LogbookPage } from "@/pages/LogbookPage";
import { VerificationPage } from "@/pages/VerificationPage";
import { BrokersPage } from "@/pages/BrokersPage";
import { ControlPage } from "@/pages/ControlPage";
import type { PageKey } from "@/pages/registry";

const PAGE_COMPONENTS: Record<PageKey, ComponentType> = {
  dashboard: DashboardPage,
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

function goPortal(portal: Portal): void {
  navigate(portalPath(portal));
}

function BootSplash(): ReactNode {
  return (
    <div className="nx-boot" role="status" aria-label="Hydrating the sovereign cockpit">
      <div className="nx-boot-inner">
        <span className="nx-kicker">NIOS</span>
        <p className="nx-xs nx-muted" style={{ letterSpacing: "0.16em" }}>
          HYDRATING FOUR-RING COCKPIT
        </p>
        <span className="nx-boot-rule" />
      </div>
    </div>
  );
}

export function App(): ReactNode {
  const location = useLocation();
  const { session, status, loading } = useSession();

  const targetPath = useMemo(() => {
    if (location.workspace === "live") return pagePath(location.page, "live");
    if (location.workspace === "paper") return pagePath(location.page, "paper");
    return pagePath(location.page, "base");
  }, [location.page, location.workspace]);

  useEffect(() => {
    if (location.portal || loading) return;
    if (location.workspace === "live" && session?.tier !== "LIVE") {
      navigate("/live/login", { replace: true });
      return;
    }
    if (location.workspace === "paper" && !session) {
      navigate("/paper/login", { replace: true });
      return;
    }
    if (location.workspace === "base" && !session) {
      navigate(status && status.initialized === false ? "/setup" : "/login", { replace: true });
    }
  }, [location.portal, location.workspace, loading, session, status]);

  if (location.portal === "setup")
    return (
      <>
        <SetupPortal onPortal={goPortal} />
        <ToastHost />
      </>
    );
  if (location.portal === "login")
    return (
      <>
        <GatewayPortal onPortal={goPortal} />
        <ToastHost />
      </>
    );
  if (location.portal === "paper-login")
    return (
      <>
        <PaperLoginPortal onPortal={goPortal} />
        <ToastHost />
      </>
    );
  if (location.portal === "live-login")
    return (
      <>
        <LiveLoginPortal onPortal={goPortal} />
        <ToastHost />
      </>
    );
  if (location.portal === "live-unlock")
    return (
      <>
        <LiveUnlockModal onPortal={goPortal} />
        <ToastHost />
      </>
    );

  if (loading) return <BootSplash />;

  const Page = PAGE_COMPONENTS[location.page];

  const shellNavigate = (page: PageKey) => {
    const workspace =
      location.workspace === "live" ? "live" : location.workspace === "paper" ? "paper" : "base";
    navigate(pagePath(page, workspace));
  };

  const chrome = (
    <>
      <CommandPalette onNavigate={shellNavigate} />
      <ToastHost />
      <button type="button" className="sr-only" onClick={openPalette} aria-label="Open command palette">
        Open command palette
      </button>
    </>
  );

  if (location.workspace === "live") {
    return (
      <>
        <LiveShell page={location.page} onNavigate={shellNavigate} onPortal={goPortal}>
          <Page />
        </LiveShell>
        {chrome}
        <span className="sr-only" aria-live="polite">
          {targetPath}
        </span>
      </>
    );
  }

  return (
    <>
      <PaperShell page={location.page} onNavigate={shellNavigate} onPortal={goPortal}>
        <Page />
      </PaperShell>
      {chrome}
      <span className="sr-only" aria-live="polite">
        {targetPath}
      </span>
    </>
  );
}
