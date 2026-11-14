/**
 * Shared portal scaffold for the four sovereign onboarding screens.
 *
 * The card side carries the form; the aside carries the contract. Paper and
 * live pass different accents and different asides, so the two doors never look
 * or read like the same surface.
 */

import type { ReactNode } from "react";

export interface PortalScaffoldProps {
  mode: "paper" | "live" | "neutral";
  eyebrow: string;
  title: string;
  subtitle: string;
  children: ReactNode;
  aside: ReactNode;
  footer?: ReactNode;
}

export function PortalScaffold({ mode, eyebrow, title, subtitle, children, aside, footer }: PortalScaffoldProps): ReactNode {
  const modeClass = mode === "paper" ? "nios-shell-paper" : mode === "live" ? "nios-shell-live" : "nios-shell-paper";
  return (
    <div className={`portal portal-split ${modeClass}`}>
      <div className="portal-body">
        <div className="portal-card rise">
          <header className="mb-5">
            <div className="row mb-3">
              <span className="brand-mark" aria-hidden="true">
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <ellipse cx="12" cy="12" rx="10" ry="5.4" stroke="var(--accent)" strokeWidth="1.3" />
                  <ellipse cx="12" cy="12" rx="6.6" ry="3.4" stroke="var(--accent-2)" strokeWidth="1.1" />
                  <circle cx="12" cy="12" r="1.5" fill="var(--accent-2)" />
                </svg>
              </span>
              <div>
                <p className="brand-title">NIOS</p>
                <p className="brand-sub">four-ring sovereign cockpit</p>
              </div>
              <span className="pill pill-accent ml-auto">v3.5.0-V1.4</span>
            </div>
            <p className="eyebrow">{eyebrow}</p>
            <h1 className="mt-1 text-[24px] leading-tight font-semibold tracking-[-0.02em]">{title}</h1>
            <p className="mt-1.5 text-[12.5px] leading-relaxed text-[var(--muted)]">{subtitle}</p>
          </header>
          {children}
          <footer className="mt-5 border-t border-[var(--line)] pt-3">
            {footer ?? (
              <p className="mono text-[9px] leading-relaxed tracking-[0.06em] text-[var(--dim)] uppercase">
                15 production pages · clean slate by default · no broker key ever reaches Ring 2
              </p>
            )}
          </footer>
        </div>
      </div>
      <aside className="portal-aside">{aside}</aside>
    </div>
  );
}
