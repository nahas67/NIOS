/**
 * `/login` — the dual-portal gateway.
 *
 * Two architectural doors, deliberately asymmetric: a cyan/lavender isometric
 * cube into the simulated sandbox and a crimson/amber armoured vault lock into
 * the real-capital workspace. The gateway never authenticates by itself.
 */

import type { ReactNode } from "react";
import { ArrowRight, Boxes, LockKeyhole, ShieldCheck } from "lucide-react";
import type { Portal } from "@/lib/router";
import { useSession } from "@/lib/session";
import { StatusPill } from "@/design-system/primitives";

export interface GatewayPortalProps {
  onPortal: (portal: Portal) => void;
}

export function GatewayPortal({ onPortal }: GatewayPortalProps): ReactNode {
  const { status } = useSession();
  const initialized = status?.initialized !== false;

  return (
    <div className="portal nios-shell-paper">
      <div className="mx-auto flex min-h-screen w-full max-w-[1240px] flex-col justify-center gap-10 px-6 py-12">
        <header className="rise">
          <div className="row">
            <span className="brand-mark" aria-hidden="true">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <ellipse cx="12" cy="12" rx="10" ry="5.4" stroke="var(--accent)" strokeWidth="1.3" />
                <ellipse cx="12" cy="12" rx="6.6" ry="3.4" stroke="var(--accent-2)" strokeWidth="1.1" />
                <circle cx="12" cy="12" r="1.5" fill="var(--accent-2)" />
              </svg>
            </span>
            <div>
              <p className="brand-title">NIOS</p>
              <p className="brand-sub">dual-portal gateway</p>
            </div>
            <span className="pill pill-accent ml-auto">v3.5.0-V1.4</span>
            <StatusPill value={initialized ? "OPERATOR INITIALISED" : "FIRST-RUN SETUP REQUIRED"} tone={initialized ? "ok" : "warn"} />
          </div>
          <h1 className="mt-6 max-w-[22ch] text-[34px] leading-[1.08] font-semibold tracking-[-0.03em]">
            Two doors. One four-ring kernel. Choose the risk you intend to carry.
          </h1>
          <p className="mt-3 max-w-[68ch] text-[13.5px] leading-relaxed text-[var(--muted)]">
            The paper sandbox and the live vault share the same deterministic kernel, the same 98% conformal gate and the
            same 30-fill nursery — they do not share credentials, capital or a session lease.
          </p>
        </header>

        <div className="grid gap-5 md:grid-cols-2">
          <button
            type="button"
            className="portal-door rise nios-shell-paper"
            style={{ ["--door-accent" as string]: "#22d3ee" }}
            onClick={() => onPortal("paper-login")}
            aria-label="Enter the paper sandbox"
          >
            <div className="row-between">
              <span className="door-title">Paper sandbox</span>
              <Boxes size={18} className="text-[#22d3ee]" aria-hidden="true" />
            </div>
            <IsometricCube />
            <p className="door-headline">ENTER PAPER SANDBOX</p>
            <p className="text-[12px] leading-relaxed text-[var(--muted)]">
              Simulated capital only. Deposit paper funds, replay the 30-fill nursery, burst shadow fills and reset every
              value to zero at will. Nothing here can reach a broker.
            </p>
            <div className="chip-row">
              <StatusPill value="zero real risk" tone="accent" />
              <StatusPill value="12h session" tone="accent2" />
              <StatusPill value="reset anytime" tone="neutral" />
            </div>
            <span className="row mt-1 text-[11px] font-semibold text-[#22d3ee]">
              Open sandbox <ArrowRight size={12} aria-hidden="true" />
            </span>
          </button>

          <button
            type="button"
            className="portal-door rise nios-shell-live"
            style={{ ["--door-accent" as string]: "#f43f5e", animationDelay: "70ms" }}
            onClick={() => onPortal("live-login")}
            aria-label="Enter the live sovereign vault"
          >
            <div className="row-between">
              <span className="door-title">Live sovereign vault</span>
              <LockKeyhole size={18} className="text-[#f43f5e]" aria-hidden="true" />
            </div>
            <VaultDial />
            <p className="door-headline">ENTER LIVE SOVEREIGN VAULT</p>
            <p className="text-[12px] leading-relaxed text-[var(--muted)]">
              Three-factor door: password, six-digit live PIN and an explicit real-capital acknowledgement. The session
              lease expires after 30 minutes and every order is Ed25519-signed by Ring 0.
            </p>
            <div className="chip-row">
              <StatusPill value="real capital" tone="bad" />
              <StatusPill value="3-factor door" tone="warn" />
              <StatusPill value="22-gate pre-flight" tone="warn" />
            </div>
            <span className="row mt-1 text-[11px] font-semibold text-[#f43f5e]">
              Open vault <ArrowRight size={12} aria-hidden="true" />
            </span>
          </button>
        </div>

        <footer className="row-between flex-wrap gap-3 border-t border-[var(--line)] pt-4">
          <p className="mono text-[9.5px] tracking-[0.08em] text-[var(--dim)] uppercase">
            15 production pages · clean slate by default · air-gapped ring 2
          </p>
          <div className="flex items-center gap-2">
            <ShieldCheck size={13} className="text-[var(--pos)]" aria-hidden="true" />
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              onClick={() => onPortal("setup")}
              aria-label="Open first-run sovereign operator setup"
            >
              First-run setup
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}

function IsometricCube(): ReactNode {
  return (
    <svg viewBox="0 0 220 92" className="w-full" role="img" aria-label="Isometric cube representing the paper sandbox">
      <g transform="translate(110 46)">
        <path d="M0,-30 L44,-6 L0,18 L-44,-6 Z" fill="rgba(34,211,238,0.14)" stroke="#22d3ee" strokeWidth="1.2" />
        <path d="M-44,-6 L0,18 L0,38 L-44,14 Z" fill="rgba(167,139,250,0.1)" stroke="rgba(167,139,250,0.8)" strokeWidth="1" />
        <path d="M44,-6 L0,18 L0,38 L44,14 Z" fill="rgba(34,211,238,0.07)" stroke="rgba(34,211,238,0.7)" strokeWidth="1" />
        <circle cx="0" cy="-6" r="3" fill="#22d3ee" />
      </g>
    </svg>
  );
}

function VaultDial(): ReactNode {
  return (
    <svg viewBox="0 0 220 92" className="w-full" role="img" aria-label="Armoured vault door dial representing the live vault">
      <g transform="translate(110 46)">
        <circle r="38" fill="rgba(244,63,94,0.08)" stroke="rgba(244,63,94,0.85)" strokeWidth="1.4" />
        <circle r="27" fill="none" stroke="rgba(245,158,11,0.55)" strokeWidth="1" strokeDasharray="4 5" />
        <circle r="5" fill="#f43f5e" />
        {[0, 90, 180, 270].map((angle) => (
          <line
            key={angle}
            x1="0"
            y1="0"
            x2="0"
            y2="-38"
            stroke="rgba(245,158,11,0.7)"
            strokeWidth="2.4"
            strokeLinecap="round"
            transform={`rotate(${angle})`}
            opacity={angle === 0 ? 0.9 : 0.35}
          />
        ))}
      </g>
    </svg>
  );
}
