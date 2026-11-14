/**
 * Flat four-ring topology.
 *
 * A technical ladder, not an illustration: four zones on the module surface,
 * one 1.5px spine, a hairline separator per zone and a single accent bar on the
 * zone the operator is currently inspecting.
 */

import type { ReactNode } from "react";

export interface RingTopologyProps {
  /** Which ring the cockpit is currently bound to (`ring0`..`ring3`). */
  active?: string;
  height?: number;
  posture?: Partial<Record<string, string>>;
}

const RINGS = [
  {
    id: "ring3",
    label: "RING 3",
    title: "Glass Cockpit",
    detail: "FastAPI control plane - REST/SSE - kill switch < 1.0 ms",
    tag: "HUMAN",
  },
  {
    id: "ring2",
    label: "RING 2",
    title: "Bicameral Brain",
    detail: "480+ factors - SHA-256 grounding - MiroFish cascade",
    tag: "COGNITION",
  },
  {
    id: "ring1",
    label: "RING 1",
    title: "Spinal Reflex",
    detail: "Tick-to-intent 0.14 ms p99 - 25k ticks/s/core",
    tag: "REFLEX",
  },
  {
    id: "ring0",
    label: "RING 0",
    title: "Sovereign Vault",
    detail: "TigerBeetle outbox - Ed25519 signing - conformal gate",
    tag: "CAPITAL",
  },
] as const;

export function RingTopology({ active = "ring3", height, posture }: RingTopologyProps): ReactNode {
  const rowH = 46;
  const gap = 8;
  const top = 14;
  const total = top * 2 + RINGS.length * rowH + (RINGS.length - 1) * gap;

  return (
    <svg
      className="nx-chart"
      viewBox={`0 0 420 ${total}`}
      style={{ height: height ?? total }}
      role="img"
      aria-label="Four-ring isolation topology"
      preserveAspectRatio="xMidYMin meet"
    >
      <title>Four-ring isolation topology</title>
      {/* spine */}
      <line
        x1={16}
        y1={top}
        x2={16}
        y2={total - top}
        stroke="#27292E"
        strokeWidth={1.5}
        shapeRendering="crispEdges"
      />
      {RINGS.map((ring, index) => {
        const y = top + index * (rowH + gap);
        const isActive = ring.id === active;
        const status = posture?.[ring.id] ?? "ISOLATED";
        return (
          <g key={ring.id} className="nx-ring-node">
            <circle
              cx={16}
              cy={y + rowH / 2}
              r={3.5}
              fill={isActive ? "#F96316" : "#16181C"}
              stroke={isActive ? "#F96316" : "#27292E"}
              strokeWidth={1.5}
            />
            <rect
              x={32}
              y={y}
              width={376}
              height={rowH}
              rx={6}
              fill={isActive ? "#22242A" : "#16181C"}
              stroke={isActive ? "#3A3D44" : "#27292E"}
              strokeWidth={1}
            />
            {isActive ? (
              <rect x={32} y={y + 5} width={2} height={rowH - 10} rx={2} fill="#F96316" />
            ) : null}
            <text x={45} y={y + 17} fill="#5A5E66" fontSize={8.5} letterSpacing="0.12em">
              {ring.label}
            </text>
            <text x={45} y={y + 31} fill="#F4F5F7" fontSize={11} fontWeight={600}>
              {ring.title}
            </text>
            <text x={196} y={y + 19} fill="#8C9099" fontSize={9}>
              {ring.detail}
            </text>
            <text
              x={196}
              y={y + 32}
              fill={isActive ? "#F96316" : "#5A5E66"}
              fontSize={8.5}
              letterSpacing="0.1em"
            >
              {ring.tag} - {status}
            </text>
            <text x={399} y={y + 26} fill="#5A5E66" fontSize={8.5} textAnchor="end">
              {String(4 - index)}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
