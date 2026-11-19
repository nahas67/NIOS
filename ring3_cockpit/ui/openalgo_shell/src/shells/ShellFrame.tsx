/**
 * Shared chrome for both workspaces.
 *
 * The sidebar, mobile nav and footer are identical; the two shells differ in
 * their top bar, their guardrails and their permitted controls.
 */

import type { ReactNode } from "react";
import { Badge, StatusDot, cx } from "../components/ui";
import { Icon } from "../components/Icon";
import { TelemetryStrip } from "../components/telemetry";
import { GROUPS, WORKSTATIONS, type Workspace, workspacePath } from "../config/nav";
import { Link, usePathname } from "../lib/router";
import { sessionFingerprint } from "../lib/api";
import { duration } from "../lib/format";
import { useSession, useSessionTtl } from "../state/session";

export function ShellFrame({
  workspace,
  topBar,
  children,
}: {
  workspace: Workspace;
  topBar: ReactNode;
  children: ReactNode;
}) {
  const pathname = usePathname();
  const { session } = useSession();
  const ttl = useSessionTtl();
  const live = workspace === "live";

  return (
    <div className="flex h-screen w-full overflow-hidden bg-canvas text-ink">
      <aside className="hidden w-[206px] shrink-0 flex-col border-r border-hairline bg-surface lg:flex">
        <div className="border-b border-hairline px-3 py-3">
          <div className="flex items-center gap-2">
            <span className={cx("h-4 w-[2px]", live ? "bg-bear" : "bg-accent")} />
            <span className="text-[13px] font-semibold tracking-[0.22em] text-ink">NIOS</span>
            <span className="ml-auto text-[8.5px] uppercase tracking-[0.1em] text-ink-3">V2.2</span>
          </div>
          <div className="mt-1 text-[8.5px] uppercase tracking-[0.16em] text-ink-3">Institutional Terminal</div>
          <div className="mt-2">
            <Badge tone={live ? "bear" : "accent"}>{live ? "LIVE VAULT" : "PAPER SANDBOX"}</Badge>
          </div>
        </div>

        <nav className="nios-scroll min-h-0 flex-1 px-1.5 py-2">
          {GROUPS.map((group) => (
            <div key={group} className="mb-2">
              <div className="px-1.5 pb-1 text-[8.5px] font-semibold uppercase tracking-[0.18em] text-ink-3">{group}</div>
              {WORKSTATIONS.filter((station) => station.group === group).map((station) => {
                const href = workspacePath(workspace, station.key);
                const active = pathname === href;
                return (
                  <Link
                    key={station.key}
                    to={href}
                    title={station.blurb}
                    className={cx(
                      "relative mb-[1px] flex items-center gap-2 rounded-[4px] px-1.5 py-[5px] text-[11px] transition-colors",
                      active ? "bg-elevated text-ink" : "text-ink-2 hover:bg-elevated hover:text-ink",
                    )}
                  >
                    {active ? <span className={cx("absolute left-0 top-1 bottom-1 w-[2px]", live ? "bg-bear" : "bg-accent")} /> : null}
                    <Icon name={station.icon} size={13} className={active ? (live ? "text-bear" : "text-accent") : "text-ink-3"} />
                    <span className="truncate">{station.label}</span>
                  </Link>
                );
              })}
            </div>
          ))}
        </nav>

        <div className="shrink-0 border-t border-hairline px-3 py-2">
          <div className="text-[8.5px] uppercase tracking-[0.16em] text-ink-3">Operator</div>
          <div className="truncate text-[11px] font-medium text-ink">{session?.username ?? "UNSIGNED"}</div>
          <div className="mt-1.5 flex items-center justify-between text-[9px] uppercase tracking-[0.1em] text-ink-3">
            <span>TTL</span>
            <span className="tabular-nums text-ink-2">{duration(ttl)}</span>
          </div>
          <div className="flex items-center justify-between text-[9px] uppercase tracking-[0.1em] text-ink-3">
            <span>CSRF</span>
            <span className="truncate font-mono text-[9px] text-ink-3">{sessionFingerprint()}</span>
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        {topBar}
        <TelemetryStrip />
        <div className="flex shrink-0 items-center gap-1 overflow-x-auto border-b border-hairline bg-surface px-2 py-1 lg:hidden">
          {WORKSTATIONS.map((station) => {
            const href = workspacePath(workspace, station.key);
            return (
              <Link
                key={station.key}
                to={href}
                className={cx(
                  "shrink-0 rounded-[4px] px-1.5 py-[3px] text-[10px] uppercase tracking-[0.06em]",
                  pathname === href ? "bg-elevated text-ink" : "text-ink-3",
                )}
              >
                {station.label}
              </Link>
            );
          })}
        </div>
        <main className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-3">{children}</main>
        <footer className="flex h-6 shrink-0 items-center justify-between gap-3 border-t border-hairline bg-surface px-3 text-[9px] uppercase tracking-[0.14em] text-ink-3">
          <span className="truncate">
            NIOS v4.3.0-V2.3 · {workspace.toUpperCase()} WORKSPACE · 17 WORKSTATIONS
          </span>
          <span className="flex items-center gap-2 tabular-nums">
            <StatusDot tone={live ? "bear" : "accent"} />
            <span>{live ? "REAL CAPITAL" : "SIMULATED"}</span>
            <span className="text-ink-3">·</span>
            <span>UTC</span>
          </span>
        </footer>
      </div>
    </div>
  );
}

export function TopBarShell({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "bear" }) {
  return (
    <header
      className={cx(
        "flex min-h-11 shrink-0 flex-wrap items-center gap-x-3 gap-y-1.5 border-b bg-surface px-3 py-1.5",
        tone === "bear" ? "border-bear" : "border-hairline",
      )}
    >
      {children}
    </header>
  );
}

export function TopBarDivider() {
  return <span className="hidden h-4 w-px bg-hairline sm:block" />;
}

export function TopBarLabel({ label, value, tone }: { label: string; value: ReactNode; tone?: "bull" | "bear" | "accent" }) {
  return (
    <div className="min-w-0">
      <div className="text-[8.5px] uppercase tracking-[0.14em] text-ink-3">{label}</div>
      <div
        className={cx(
          "truncate text-[11.5px] font-semibold tabular-nums leading-tight",
          tone === "bull" ? "text-bull" : tone === "bear" ? "text-bear" : tone === "accent" ? "text-accent" : "text-ink",
        )}
      >
        {value}
      </div>
    </div>
  );
}

export function StreamIndicator({ status, frames }: { status: string; frames: number }) {
  const tone = status === "open" ? "bull" : status === "connecting" || status === "reconnecting" ? "accent" : status === "error" ? "bear" : "muted";
  return (
    <div className="flex items-center gap-1.5 rounded-[4px] border border-hairline bg-canvas px-1.5 py-[3px]">
      <StatusDot tone={tone} pulse={status === "open"} />
      <span className="text-[9px] font-medium uppercase tracking-[0.1em] text-ink-2">SSE</span>
      <span className="text-[9px] tabular-nums text-ink-3">{status.toUpperCase()}</span>
      <span className="text-[9px] tabular-nums text-ink-3">·</span>
      <span className="text-[9px] tabular-nums text-ink-2">{frames}</span>
    </div>
  );
}

export function AlertsButton({ count, critical, href }: { count: number; critical: number; href: string }) {
  return (
    <Link
      to={href}
      title="Incident & alert centre"
      className={cx(
        "flex items-center gap-1.5 rounded-[4px] border px-1.5 py-[3px] text-[9px] uppercase tracking-[0.1em]",
        critical > 0 ? "border-bear-border bg-bear-tint text-bear" : "border-hairline bg-canvas text-ink-2 hover:text-ink",
      )}
    >
      <Icon name="bell" size={12} />
      <span className="font-semibold">ALERTS</span>
      <span className="tabular-nums">{count}</span>
      {critical > 0 ? <span className="rounded-[2px] bg-bear px-1 text-[9px] font-bold text-[#0D0E10] tabular-nums">{critical}</span> : null}
    </Link>
  );
}
