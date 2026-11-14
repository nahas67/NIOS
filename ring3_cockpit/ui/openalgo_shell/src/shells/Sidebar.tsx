/**
 * Shared navigation rail.
 *
 * Five operational domains, live counters, an active-route indicator and a
 * collapse state persisted per browser. The rail itself is mode-agnostic: the
 * shell root supplies the accent variables.
 */

import { useEffect, useState, type ReactNode } from "react";
import {
  Activity,
  BookOpen,
  Boxes,
  Brain,
  ChevronsLeft,
  ChevronsRight,
  Command,
  FlaskConical,
  Gauge,
  Hammer,
  Layers,
  ListOrdered,
  LogOut,
  Network,
  PenLine,
  ShieldCheck,
  SlidersHorizontal,
  TrendingUp,
  Users,
} from "lucide-react";
import { NAV_GROUPS, PAGE_BY_KEY, pagesByGroup, type PageKey } from "@/pages/registry";
import { openPalette } from "@/design-system/primitives";
import type { Tier, Workspace } from "@/lib/types";

const ICONS: Record<PageKey, typeof Gauge> = {
  dashboard: Gauge,
  terminal: Activity,
  orders: ListOrdered,
  options: Layers,
  analyzer: FlaskConical,
  quant: Boxes,
  agents: Users,
  backtest: TrendingUp,
  brain: Brain,
  forge: Hammer,
  ledger: BookOpen,
  logbook: PenLine,
  verification: ShieldCheck,
  brokers: Network,
  control: SlidersHorizontal,
};

const COLLAPSE_KEY = "nios.sidebar.collapsed";

export interface SidebarProps {
  workspace: Workspace;
  active: PageKey;
  onNavigate: (page: PageKey) => void;
  badges?: Partial<Record<PageKey, string>>;
  username: string | null;
  tier: Tier;
  onSignOut: () => void;
}

export function Sidebar({ workspace, active, onNavigate, badges, username, tier, onSignOut }: SidebarProps): ReactNode {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return window.localStorage.getItem(COLLAPSE_KEY) === "1";
    } catch {
      return false;
    }
  });

  useEffect(() => {
    try {
      window.localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
    } catch {
      /* storage disabled */
    }
  }, [collapsed]);

  const workspaceLabel = workspace === "paper" ? "PAPER SANDBOX" : workspace === "live" ? "LIVE VAULT" : "OPERATOR";

  return (
    <aside className="sidebar" aria-label="Primary navigation">
      <div className="sidebar-brand">
        <span className="brand-mark" aria-hidden="true">
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <ellipse cx="12" cy="12" rx="10" ry="5.4" stroke="var(--accent)" strokeWidth="1.3" />
            <ellipse cx="12" cy="12" rx="6.6" ry="3.4" stroke="var(--accent-2)" strokeWidth="1.1" />
            <circle cx="12" cy="12" r="1.5" fill="var(--accent-2)" />
          </svg>
        </span>
        <div className="brand-text">
          <p className="brand-title">NIOS</p>
          <p className="brand-sub">{workspaceLabel}</p>
        </div>
      </div>

      <div className="sidebar-scroll">
        <button
          type="button"
          className="nav-item"
          onClick={openPalette}
          aria-label="Open quick jump command palette (Control or Command plus K)"
          title="Quick jump · Ctrl/Cmd K"
        >
          <Command size={14} className="nav-icon" aria-hidden="true" />
          <span className="nav-label">Quick jump</span>
          <span className="nav-count">⌘K</span>
        </button>

        {NAV_GROUPS.map((group) => (
          <nav className="nav-group" key={group.id} aria-label={group.label}>
            <p className="nav-group-label">
              {group.roman}. {group.label}
            </p>
            {pagesByGroup(group.id).map((page) => {
              const Icon = ICONS[page.key];
              const current = page.key === active;
              return (
                <button
                  key={page.key}
                  type="button"
                  className="nav-item"
                  aria-current={current ? "page" : undefined}
                  aria-label={page.title}
                  title={collapsed ? page.title : undefined}
                  onClick={() => onNavigate(page.key)}
                >
                  <Icon size={14} className="nav-icon" aria-hidden="true" />
                  <span className="nav-label truncate">{PAGE_BY_KEY[page.key].short}</span>
                  {badges?.[page.key] ? <span className="nav-count">{badges[page.key]}</span> : null}
                </button>
              );
            })}
          </nav>
        ))}
      </div>

      <div className="sidebar-foot">
        <div className="min-w-0 sidebar-foot-text">
          <p className="mono truncate text-[10.5px] text-[var(--ink-soft)]" title={username ?? "operator"}>
            {username ?? "operator"}
          </p>
          <p className="mono text-[8.5px] tracking-[0.12em] text-[var(--dim)] uppercase">{tier} TIER</p>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            className="btn btn-ghost btn-icon"
            onClick={onSignOut}
            aria-label="Sign out of this workspace"
            title="Sign out"
          >
            <LogOut size={13} />
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-icon"
            onClick={() => setCollapsed((value) => !value)}
            aria-label={collapsed ? "Expand navigation rail" : "Collapse navigation rail"}
            aria-expanded={!collapsed}
            title={collapsed ? "Expand rail" : "Collapse rail"}
          >
            {collapsed ? <ChevronsRight size={13} /> : <ChevronsLeft size={13} />}
          </button>
        </div>
      </div>
    </aside>
  );
}
