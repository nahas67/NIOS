/**
 * Bespoke isometric 4-ring visualiser.
 *
 * Four stacked isometric plates, each with an extruded edge, a heartbeat dot and
 * the owning process. Ring 0 (vault) sits at the top of the stack and Ring 3
 * (cockpit) at the base, separated by dashed air-gap links so the isolation
 * contract is visible at a glance.
 */

import type { ReactNode } from "react";
import { num } from "@/lib/format";
import { StatusPill, type PillTone } from "@/design-system/primitives";

export interface RingVisual {
  index: number;
  label: string;
  status: string;
  alive: boolean;
  pid?: number | null;
  heartbeatMs?: number | null;
  detail?: string;
}

export interface IsometricRingsProps {
  rings: RingVisual[];
  height?: number;
}

const RING_COLORS = ["#f43f5e", "#22d3ee", "#a78bfa", "#f59e0b"];

function toneFor(ring: RingVisual): PillTone {
  if (!ring.alive) return "bad";
  const status = ring.status.toUpperCase();
  if (status.includes("HALT") || status.includes("FROZEN")) return "bad";
  if (status.includes("DEGRAD") || status.includes("WARN")) return "warn";
  return "ok";
}

export function IsometricRings({ rings, height = 300 }: IsometricRingsProps): ReactNode {
  const width = 560;
  const ordered = [...rings].sort((a, b) => a.index - b.index);
  const plateW = 300;
  const plateH = 118;
  const step = Math.min(64, (height - 70) / Math.max(1, ordered.length - 1));
  const baseY = height - 74;

  return (
    <div className="relative w-full">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        height={height}
        role="img"
        aria-label={`Isometric four-ring isolation diagram: ${ordered
          .map((ring) => `Ring ${ring.index} ${ring.label} ${ring.alive ? "alive" : "down"}`)
          .join(", ")}`}
      >
        <defs>
          <linearGradient id="iso-base" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="rgba(148,163,184,0.1)" />
            <stop offset="100%" stopColor="rgba(148,163,184,0.02)" />
          </linearGradient>
        </defs>

        <rect x="0" y="0" width={width} height={height} fill="url(#iso-base)" className="nios-iso-plane" rx="14" />

        <g opacity="0.9">
          {ordered.map((ring, position) => {
            const y = baseY - position * step;
            const scaleFactor = 0.78 + position * 0.075;
            const w = plateW * scaleFactor;
            const h = plateH * scaleFactor;
            const color = RING_COLORS[ring.index % RING_COLORS.length];
            const diamond = `M0,${-h / 2} L${w / 2},0 L0,${h / 2} L${-w / 2},0 Z`;
            const depth = 12;

            return (
              <g key={ring.index} transform={`translate(${width / 2 - 34}, ${y})`}>
                {position < ordered.length - 1 ? (
                  <line
                    x1={-38}
                    y1={h / 2 + 2}
                    x2={-38}
                    y2={h / 2 + step - 4}
                    stroke="rgba(148,163,184,0.35)"
                    strokeDasharray="3 4"
                  />
                ) : null}

                <path d={diamond} transform={`translate(0, ${depth})`} fill="rgba(4,7,13,0.85)" stroke="rgba(148,163,184,0.16)" strokeWidth="1" />
                <path
                  d={diamond}
                  fill="rgba(8,12,22,0.88)"
                  stroke={color}
                  strokeWidth="1.25"
                  style={{ filter: `drop-shadow(0 0 12px ${color}44)` }}
                />
                <path
                  d={`M${-w / 2},0 L0,${h / 2} L${w / 2},0`}
                  fill="none"
                  stroke={color}
                  strokeWidth="1"
                  opacity="0.45"
                />

                <text x="0" y={-6} textAnchor="middle" fill={color} style={{ fontSize: 12, fontFamily: "var(--font-mono)", letterSpacing: "0.14em" }}>
                  RING {ring.index}
                </text>
                <text x="0" y={9} textAnchor="middle" fill="var(--ink-soft)" style={{ fontSize: 10.5, fontFamily: "var(--font-sans)" }}>
                  {ring.label}
                </text>
                <circle cx={Math.max(30, w / 2 - 26)} cy={-h / 2 + 22} r={3.6} fill={ring.alive ? color : "#64748b"}>
                  {ring.alive ? (
                    <animate attributeName="opacity" values="1;0.25;1" dur="2.4s" repeatCount="indefinite" />
                  ) : null}
                </circle>
              </g>
            );
          })}
        </g>
      </svg>

      <ul className="absolute top-3 right-3 flex w-[196px] flex-col gap-1.5">
        {ordered.map((ring) => (
          <li key={ring.index} className="flex items-center justify-between gap-2 rounded-[var(--r-sm)] border border-[var(--line)] bg-[rgba(6,9,16,0.72)] px-2 py-1.5">
            <span className="mono text-[9px] tracking-[0.08em] text-[var(--dim)]">
              R{ring.index} · {ring.pid ?? "—"}
              {ring.heartbeatMs !== null && ring.heartbeatMs !== undefined ? ` · ${num(ring.heartbeatMs, 0)}ms` : ""}
            </span>
            <StatusPill value={ring.alive ? ring.status || "ALIVE" : "DOWN"} tone={toneFor(ring)} />
          </li>
        ))}
      </ul>
    </div>
  );
}
