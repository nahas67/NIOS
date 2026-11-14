/**
 * Collapsible domain rail.
 *
 * Five operational domains, fifteen workstations, one accent left indicator on
 * the active row. The collapsed state keeps every icon reachable and exposes
 * the label as the accessible name, so the rail is usable in an 8-column grid.
 */

import type { ReactNode } from "react";
import { NAV_GROUPS, PAGE_BY_KEY, type PageKey } from "@/pages/registry";
import { Glyph } from "./icons";
import type { NavProps } from "./types";

/**
 * The rail order is declared here, not derived, so a re-ordered registry can
 * never silently reshuffle an operator's muscle memory. Every domain keeps the
 * registry as the single source of truth for the route a key resolves to.
 */
const DOMAIN_KEYS: Record<string, PageKey[]> = {
  core: ["dashboard", "terminal", "orders"],
  quant: ["options", "analyzer", "quant"],
  brain: ["agents", "backtest", "brain", "forge"],
  audit: ["ledger", "logbook", "verification"],
  sovereign: ["brokers", "control"],
};

export function Sidebar({ page, collapsed, onToggle, onNavigate }: NavProps): ReactNode {
  return (
    <nav
      className={collapsed ? "nx-sidebar nx-sidebar--rail" : "nx-sidebar"}
      aria-label="Workstation domains"
    >
      <div className="nx-sidebar-brand">
        <span className="nx-sidebar-brand-mark" aria-hidden="true">
          <Glyph name="shield" size={13} />
        </span>
        {collapsed ? null : (
          <span className="nx-sidebar-brand-text">
            <span className="nx-sidebar-brand-title">NIOS</span>
            <span className="nx-sidebar-brand-sub">Sovereign Cockpit</span>
          </span>
        )}
      </div>

      <div className="nx-nav">
        {NAV_GROUPS.map((group) => (
          <div className="nx-nav-group" key={group.id}>
            <div className="nx-nav-grouphead">
              {collapsed ? (
                <span className="nx-nav-roman">{group.roman}</span>
              ) : (
                <>
                  <span>{group.label}</span>
                  <span className="nx-nav-roman">{group.roman}</span>
                </>
              )}
            </div>
            {(DOMAIN_KEYS[group.id] ?? []).map((key) => {
              const entry = PAGE_BY_KEY[key];
              const active = entry.key === page;
              return (
                <button
                  key={entry.key}
                  type="button"
                  className={active ? "nx-nav-item nx-nav-item--active" : "nx-nav-item"}
                  onClick={() => onNavigate(entry.key)}
                  aria-current={active ? "page" : undefined}
                  title={collapsed ? entry.title : undefined}
                >
                  <Glyph name={entry.key} className="nx-nav-icon" />
                  {collapsed ? (
                    <span className="sr-only">{entry.title}</span>
                  ) : (
                    <span className="nx-nav-label">{entry.short}</span>
                  )}
                </button>
              );
            })}
          </div>
        ))}
      </div>

      <div className="nx-sidebar-foot">
        <button
          type="button"
          className="nx-rail-toggle"
          onClick={onToggle}
          aria-expanded={!collapsed}
          aria-label={collapsed ? "Expand the domain rail" : "Collapse the domain rail"}
        >
          <Glyph name={collapsed ? "expand" : "collapse"} size={14} />
          {collapsed ? null : <span>Collapse rail</span>}
        </button>
      </div>
    </nav>
  );
}
