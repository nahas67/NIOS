/**
 * Gateway portal - two physically distinct doors.
 *
 * Left: the paper sandbox, which needs no capital and no PIN. Right: the live
 * sovereign vault, which advertises the hazard, the third factor and the
 * 22-gate certificate before the operator even asks for the form.
 */

import type { ReactNode } from "react";
import type { Portal } from "@/lib/router";
import { RingTopology } from "@/design-system/ring-topology";
import { StatusBadge } from "@/design-system/primitives";
import { Glyph } from "@/shells/icons";

export function GatewayPortal({ onPortal }: { onPortal: (portal: Portal) => void }): ReactNode {
  return (
    <div className="nx-portal">
      <header className="nx-portal-top">
        <span className="nx-sidebar-brand-mark" aria-hidden="true">
          N
        </span>
        <div className="nx-sidebar-brand-text">
          <span className="nx-sidebar-brand-title">NIOS</span>
          <span className="nx-sidebar-brand-sub">Institutional Financial Terminal</span>
        </div>
        <span className="nx-badge nx-badge--muted" style={{ marginLeft: "auto" }}>
          FOUR-RING AIR GAP
        </span>
      </header>

      <div className="nx-portal-body">
        <div className="nx-portal-split">
          <button type="button" className="nx-door" onClick={() => onPortal("paper-login")}>
            <div className="nx-door-head">
              <Glyph name="coin" size={18} />
              <span className="nx-door-title">PAPER SANDBOX</span>
              <StatusBadge tone="neutral" dot>
                SIMULATED
              </StatusBadge>
            </div>
            <p>
              A full mirror of the four-ring stack with simulated fills. Nothing here can reach a broker,
              sign an order or settle a currency. Start at a clean slate and fund it yourself.
            </p>
            <ul className="nx-stack" style={{ gap: 4 }}>
              <li className="nx-xs nx-dim">- Two-factor username + password</li>
              <li className="nx-xs nx-dim">- Shadow twin and 30-fill nursery enabled</li>
              <li className="nx-xs nx-dim">- Deposit, reset and autopilot transport</li>
            </ul>
            <div className="nx-door-foot">
              <span className="nx-badge nx-badge--muted">ENTER PAPER SANDBOX</span>
              <span style={{ marginLeft: "auto" }}>/paper/login</span>
            </div>
          </button>

          <button type="button" className="nx-door" onClick={() => onPortal("live-login")}>
            <div className="nx-door-head">
              <Glyph name="shield" size={18} />
              <span className="nx-door-title">LIVE SOVEREIGN VAULT</span>
              <StatusBadge tone="bear" dot>
                REAL CAPITAL
              </StatusBadge>
            </div>
            <p>
              The funded vault. Ring 0 signs every outbound order, the conformal gate must clear before a
              proposal is staged, and the global kill switch lands in under one millisecond.
            </p>
            <ul className="nx-stack" style={{ gap: 4 }}>
              <li className="nx-xs nx-dim">- Three-factor: username + password + six-digit PIN</li>
              <li className="nx-xs nx-dim">- 22-gate pre-flight certificate on entry</li>
              <li className="nx-xs nx-dim">- 30-minute lease, no deposit or reset controls</li>
            </ul>
            <div className="nx-door-foot">
              <span className="nx-badge nx-badge--bear">LIVE VAULT - PIN REQUIRED</span>
              <span style={{ marginLeft: "auto" }}>/live/login</span>
            </div>
          </button>

          <div className="nx-panel" style={{ gridColumn: "1 / -1" }}>
            <div className="nx-panel-head">
              <div className="nx-panel-title">
                <strong>Isolation posture</strong>
                <span>Ring 2 reasons about the market; Ring 0 alone can sign</span>
              </div>
            </div>
            <div className="nx-panel-body">
              <RingTopology active="ring3" posture={{ ring0: "SEALED", ring1: "ARMED", ring2: "ISOLATED", ring3: "BOUND" }} />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
