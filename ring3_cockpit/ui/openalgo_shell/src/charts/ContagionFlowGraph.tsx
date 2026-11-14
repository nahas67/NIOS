/**
 * MiroFish contagion flow.
 *
 * Five behavioural archetypes push a signed shock through three ticks. Edge
 * thickness encodes magnitude, colour encodes sign, and the fragility index is
 * drawn against its Ring 1 revocation threshold.
 */

import type { ReactNode } from "react";
import { num } from "@/lib/format";
import { StatusPill } from "@/design-system/primitives";

export interface ContagionFlowGraphProps {
  archetypes: string[];
  tickFlows?: number[];
  matrix?: number[][];
  weights?: number[];
  fragility?: number;
  threshold?: number;
  height?: number;
}

const PAD_X = 108;
const NODE_R = 15;

function normaliseMatrix(
  rowCount: number,
  columnCount: number,
  matrix: number[][] | undefined,
  tickFlows: number[] | undefined,
  weights: number[] | undefined,
): number[][] {
  if (matrix && matrix.length === rowCount && matrix.every((row) => row.length >= columnCount)) {
    return matrix.map((row) => row.slice(0, columnCount));
  }
  const weightTotal = weights && weights.length >= rowCount ? weights.slice(0, rowCount).reduce((sum, value) => sum + Math.abs(value), 0) : 0;
  return Array.from({ length: rowCount }, (_, row) =>
    Array.from({ length: columnCount }, (_, column) => {
      const flow = tickFlows?.[column] ?? 0;
      const share = weightTotal > 0 && weights ? Math.abs(weights[row] ?? 0) / weightTotal : 1 / rowCount;
      return flow * share;
    }),
  );
}

export function ContagionFlowGraph({
  archetypes,
  tickFlows = [],
  matrix,
  weights,
  fragility = 0,
  threshold = 0.75,
  height = 300,
}: ContagionFlowGraphProps): ReactNode {
  const rows = Math.max(1, archetypes.length);
  const columns = 3;
  const width = 860;
  const innerH = height - 70;
  const stepY = innerH / rows;
  const values = normaliseMatrix(rows, columns, matrix, tickFlows, weights);
  const maxAbs = Math.max(0.0001, ...values.flat().map((value) => Math.abs(value)));
  const columnX = Array.from({ length: columns }, (_, index) => PAD_X + ((width - PAD_X - 150) / columns) * (index + 1));
  const rowY = (index: number) => 44 + stepY * index + stepY / 2;
  const armed = fragility >= threshold;

  return (
    <div className="w-full">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        height={height}
        role="img"
        aria-label={`Directed contagion flow across ${rows} archetypes and three ticks. Fragility index ${num(fragility, 3)} against a Ring 1 threshold of ${num(threshold, 2)}.`}
      >
        <defs>
          <marker id="contagion-out" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M0,0 L8,4 L0,8 z" fill="rgba(251,113,133,0.85)" />
          </marker>
          <marker id="contagion-in" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M0,0 L8,4 L0,8 z" fill="rgba(245,158,11,0.85)" />
          </marker>
        </defs>

        {Array.from({ length: columns }, (_, index) => (
          <text key={`tick-${index}`} x={columnX[index]} y={24} textAnchor="middle" className="iso-ring-label" style={{ fontSize: 9 }}>
            TICK {index + 1}
          </text>
        ))}

        {archetypes.map((archetype, row) => (
          <g key={archetype}>
            <text x={12} y={rowY(row) + 3} className="iso-ring-label" style={{ fontSize: 8.5 }}>
              {archetype.replace(/_/g, " ").slice(0, 18)}
            </text>
            <text x={12} y={rowY(row) + 14} fill="rgba(148,163,184,0.55)" style={{ fontSize: 7.5, fontFamily: "var(--font-mono)" }}>
              w {num(weights?.[row] ?? 0, 2)}
            </text>
            <circle cx={PAD_X - 40} cy={rowY(row)} r={4.5} fill={values[row].some((value) => value < 0) ? "rgba(251,113,133,0.8)" : "rgba(245,158,11,0.8)"} />
          </g>
        ))}

        {values.map((row, rowIndex) =>
          row.map((value, columnIndex) => {
            const fromX = columnIndex === 0 ? PAD_X - 30 : columnX[columnIndex - 1] + NODE_R;
            const toX = columnX[columnIndex] - NODE_R;
            const fromY = columnIndex === 0 ? rowY(rowIndex) : rowY(rowIndex) + (value >= 0 ? -6 : 6);
            const toY = rowY(rowIndex) + (value >= 0 ? -6 : 6);
            const midX = (fromX + toX) / 2;
            const lift = value >= 0 ? -10 - Math.abs(value) * 0.6 : 10 + Math.abs(value) * 0.6;
            const strokeWidth = 0.6 + (Math.abs(value) / maxAbs) * 4.2;
            return (
              <path
                key={`${rowIndex}-${columnIndex}`}
                d={`M${fromX},${fromY} Q${midX},${rowY(rowIndex) + lift} ${toX},${toY}`}
                fill="none"
                stroke={value >= 0 ? "rgba(245,158,11,0.62)" : "rgba(251,113,133,0.62)"}
                strokeWidth={strokeWidth}
                markerEnd={`url(#${value >= 0 ? "contagion-in" : "contagion-out"})`}
                opacity={0.35 + (Math.abs(value) / maxAbs) * 0.6}
              >
                <title>{`${archetypes[rowIndex]} · tick ${columnIndex + 1}: ${num(value, 3)}`}</title>
              </path>
            );
          }),
        )}

        {columnX.map((x, index) => (
          <g key={`node-${index}`}>
            <circle cx={x} cy={height - 34} r={5} fill="rgba(148,163,184,0.5)" />
            <text x={x} y={height - 18} textAnchor="middle" className="iso-ring-label" style={{ fontSize: 7.5 }}>
              {num(tickFlows[index], 2)}
            </text>
          </g>
        ))}

        <text x={columnX[0]} y={height - 6} textAnchor="middle" fill="rgba(148,163,184,0.5)" style={{ fontSize: 7, fontFamily: "var(--font-mono)" }}>
          net flow per tick
        </text>
      </svg>

      <div className="mt-1 flex flex-wrap items-center gap-3">
        <span className="mono flex items-center gap-1.5 text-[9px] tracking-[0.08em] text-[var(--dim)]">
          <span className="inline-block h-[2px] w-4 rounded bg-[rgba(245,158,11,0.8)]" /> POSITIVE FLOW
        </span>
        <span className="mono flex items-center gap-1.5 text-[9px] tracking-[0.08em] text-[var(--dim)]">
          <span className="inline-block h-[2px] w-4 rounded bg-[rgba(251,113,133,0.8)]" /> NEGATIVE FLOW
        </span>
        <span className="ml-auto flex items-center gap-2">
          <span className="eyebrow">fragility</span>
          <span className="mono tnum text-[12px] text-[var(--ink)]">{num(fragility, 3)}</span>
          <StatusPill value={armed ? "RING1 HALT" : "BELOW THRESHOLD"} tone={armed ? "bad" : "ok"} />
        </span>
      </div>
    </div>
  );
}
