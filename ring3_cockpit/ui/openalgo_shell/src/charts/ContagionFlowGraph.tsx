/**
 * MiroFish contagion flow matrix.
 *
 * Five archetypes down the rows, three cascade ticks across the columns. Each
 * cell is a flat tile whose tint encodes the sign of the shock and whose label
 * carries the exact cascade-fragility contribution, with directed arrows
 * between ticks so the propagation order is unambiguous.
 */

import type { ReactNode } from "react";
import { CHART, ChartLegend } from "./kit";

export interface ContagionFlowGraphProps {
  /** matrix[archetypeIndex][tickIndex], normalised to -1..1. */
  matrix: number[][];
  archetypes?: string[];
  ticks?: string[];
  height?: number;
  fragility?: number;
}

const DEFAULT_ARCHETYPES = ["PANIC", "GREED", "HERD", "CONTRARIAN", "LIQUIDITY"];
const DEFAULT_TICKS = ["T+0", "T+1", "T+2"];
const TILE_W = 112;
const TILE_H = 34;
const GAP_X = 42;
const GAP_Y = 9;
const LEFT = 104;

export function ContagionFlowGraph({
  matrix,
  archetypes = DEFAULT_ARCHETYPES,
  ticks = DEFAULT_TICKS,
  height,
  fragility = 0,
}: ContagionFlowGraphProps): ReactNode {
  const rows = archetypes.length;
  const cols = ticks.length;
  const width = LEFT + cols * TILE_W + (cols - 1) * GAP_X + 24;
  const total = 22 + rows * TILE_H + (rows - 1) * GAP_Y + 16;

  const value = (row: number, col: number): number => {
    const line = matrix[row];
    if (!Array.isArray(line)) return 0;
    const cell = line[col];
    return typeof cell === "number" && Number.isFinite(cell) ? cell : 0;
  };

  return (
    <div className="nx-stack">
      <svg
        className="nx-chart"
        viewBox={`0 0 ${width} ${total}`}
        style={{ height: height ?? total }}
        role="img"
        aria-label="MiroFish contagion flow matrix"
        preserveAspectRatio="xMinYMin meet"
      >
        <title>MiroFish five-archetype three-tick contagion matrix</title>

        {ticks.map((tick, col) => (
          <text
            key={tick}
            x={LEFT + col * (TILE_W + GAP_X) + TILE_W / 2}
            y={12}
            textAnchor="middle"
            fill={CHART.axis}
            fontSize={8.5}
            letterSpacing="0.1em"
          >
            {tick}
          </text>
        ))}

        <text x={LEFT - 10} y={12} textAnchor="end" fill={CHART.axis} fontSize={8.5}>
          FRAGILITY {fragility.toFixed(3)}
        </text>

        {archetypes.map((archetype, row) => {
          const y = 22 + row * (TILE_H + GAP_Y);
          return (
            <g key={archetype}>
              <text x={LEFT - 10} y={y + TILE_H / 2 + 3} textAnchor="end" fill={CHART.text} fontSize={9}>
                {archetype}
              </text>
              {ticks.map((tick, col) => {
                const cell = value(row, col);
                const bullish = cell > 0.02;
                const bearish = cell < -0.02;
                const x = LEFT + col * (TILE_W + GAP_X);
                return (
                  <g key={`${archetype}-${tick}`}>
                    <rect
                      x={x}
                      y={y}
                      width={TILE_W}
                      height={TILE_H}
                      rx={4}
                      fill={bullish ? CHART.bullFill : bearish ? CHART.bearFill : CHART.surface}
                      stroke={bullish ? CHART.bullLine : bearish ? CHART.bearLine : CHART.grid}
                      strokeWidth={1}
                    />
                    <text x={x + 8} y={y + 14} fill={CHART.axis} fontSize={7.5} letterSpacing="0.08em">
                      {tick}
                    </text>
                    <text
                      x={x + 8}
                      y={y + 26}
                      fill={bullish ? CHART.bull : bearish ? CHART.bear : CHART.text}
                      fontSize={10}
                      fontWeight={600}
                    >
                      {cell >= 0 ? "+" : ""}
                      {cell.toFixed(2)}
                    </text>
                    <line
                      x1={x + TILE_W - 46}
                      y1={y + TILE_H - 12}
                      x2={x + TILE_W - 10}
                      y2={y + TILE_H - 12}
                      stroke={CHART.grid}
                      strokeWidth={1}
                    />
                    <rect
                      x={x + TILE_W - 46}
                      y={y + TILE_H - 13}
                      width={Math.max(1, Math.min(36, Math.abs(cell) * 36))}
                      height={3}
                      fill={bullish ? CHART.bull : bearish ? CHART.bear : CHART.axis}
                    />
                  </g>
                );
              })}
              {cols > 1
                ? Array.from({ length: cols - 1 }).map((_, col) => {
                    const arrowX = LEFT + col * (TILE_W + GAP_X) + TILE_W + 6;
                    const arrowY = y + TILE_H / 2;
                    return (
                      <g key={`flow-${archetype}-${col}`}>
                        <line
                          x1={arrowX}
                          y1={arrowY}
                          x2={arrowX + GAP_X - 16}
                          y2={arrowY}
                          stroke={CHART.grid}
                          strokeWidth={1.5}
                        />
                        <path
                          d={`M${arrowX + GAP_X - 16} ${arrowY} l-4 -3 l0 6 Z`}
                          fill={CHART.axis}
                        />
                      </g>
                    );
                  })
                : null}
            </g>
          );
        })}
      </svg>
      <ChartLegend
        items={[
          { label: "Adverse propagation", color: CHART.bearLine },
          { label: "Stabilising flow", color: CHART.bullLine },
          { label: "Neutral tick", color: CHART.grid },
        ]}
      />
    </div>
  );
}
