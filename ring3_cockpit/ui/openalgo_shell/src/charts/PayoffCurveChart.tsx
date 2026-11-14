/**
 * Multi-leg expiry payoff.
 *
 * Twenty-five sampled spots across the leg envelope. The profit zone above the
 * zero line is the forest tint, the loss zone below is the burgundy tint, and
 * every breakeven crossing gets a hairline marker.
 */

import type { ReactNode } from "react";
import { CHART, ChartLegend, extent, linePath, plotGrid, scale } from "./kit";

export interface PayoffPoint {
  spot: number;
  pnl: number;
}

export interface PayoffCurveChartProps {
  points: PayoffPoint[];
  breakevens?: number[];
  height?: number;
  strategy?: string;
}

const BOX = { x: 56, y: 14, w: 632, h: 212 };

export function PayoffCurveChart({
  points,
  breakevens = [],
  height = 260,
  strategy = "MULTI-LEG",
}: PayoffCurveChartProps): ReactNode {
  const clean = points.slice(0, 25);

  if (clean.length < 2) {
    return (
      <div className="nx-stack">
        <svg className="nx-chart" viewBox="0 0 720 260" style={{ height }} role="img" aria-label="Payoff curve empty">
          <title>Expiry payoff - no legs</title>
          <rect x={BOX.x} y={BOX.y} width={BOX.w} height={BOX.h / 2} fill={CHART.bullFill} />
          <rect x={BOX.x} y={BOX.y + BOX.h / 2} width={BOX.w} height={BOX.h / 2} fill={CHART.bearFill} />
          {plotGrid(BOX)}
          <text x={BOX.x + BOX.w / 2} y={BOX.y + BOX.h / 2} textAnchor="middle" fill={CHART.axis} fontSize={11}>
            NO LEGS - add a structure to price the expiry envelope
          </text>
        </svg>
        <ChartLegend
          items={[
            { label: "Profit zone", color: CHART.bullLine },
            { label: "Loss zone", color: CHART.bearLine },
          ]}
        />
      </div>
    );
  }

  const spots = clean.map((point) => point.spot);
  const pnls = clean.map((point) => point.pnl);
  const [spotLo, spotHi] = extent(spots);
  const [pnlLo, pnlHi] = extent(pnls, [-1, 1]);
  const pad = (pnlHi - pnlLo) * 0.12 || 1;
  const y = scale(pnlLo - pad, pnlHi + pad, BOX.h);
  const x = scale(spotLo, spotHi, BOX.w);
  const zeroY = BOX.y + BOX.h - y(0);
  const zf = Math.max(0.02, Math.min(0.98, (zeroY - BOX.y) / BOX.h));

  const path = linePath(clean.map((point) => [BOX.x + x(point.spot), BOX.y + BOX.h - y(point.pnl)] as [number, number]));

  const crossings: number[] = [];
  for (let index = 1; index < clean.length; index += 1) {
    const prev = clean[index - 1];
    const current = clean[index];
    if (prev.pnl === 0) continue;
    if ((prev.pnl < 0 && current.pnl > 0) || (prev.pnl > 0 && current.pnl < 0)) {
      const weight = Math.abs(prev.pnl) / (Math.abs(prev.pnl) + Math.abs(current.pnl));
      crossings.push(prev.spot + (current.spot - prev.spot) * weight);
    }
  }

  return (
    <div className="nx-stack">
      <svg className="nx-chart" viewBox="0 0 720 260" style={{ height }} role="img" aria-label={`${strategy} expiry payoff`}>
        <title>{`${strategy} expiry payoff`}</title>
        {/* flat profit / loss zones, never a gradient */}
        <rect x={BOX.x} y={BOX.y} width={BOX.w} height={BOX.h * zf} fill={CHART.bullFill} />
        <rect x={BOX.x} y={BOX.y + BOX.h * zf} width={BOX.w} height={BOX.h * (1 - zf)} fill={CHART.bearFill} />
        {plotGrid(BOX)}
        <line
          x1={BOX.x}
          y1={zeroY}
          x2={BOX.x + BOX.w}
          y2={zeroY}
          stroke={CHART.text}
          strokeWidth={1}
          strokeDasharray="4 3"
        />
        <text x={BOX.x - 6} y={zeroY + 3} textAnchor="end" fill={CHART.text} fontSize={8.5}>
          0.00
        </text>
        <text x={BOX.x - 6} y={BOX.y + 4} textAnchor="end" fill={CHART.bull} fontSize={8.5}>
          {pnlHi.toFixed(0)}
        </text>
        <text x={BOX.x - 6} y={BOX.y + BOX.h + 3} textAnchor="end" fill={CHART.bear} fontSize={8.5}>
          {pnlLo.toFixed(0)}
        </text>

        <path d={path} fill="none" stroke={CHART.strong} strokeWidth={1.5} />

        {[...crossings, ...breakevens].map((value, index) => {
          const cx = BOX.x + x(value);
          if (!Number.isFinite(cx) || cx < BOX.x || cx > BOX.x + BOX.w) return null;
          return (
            <g key={`be-${index}-${value.toFixed(3)}`}>
              <line x1={cx} y1={BOX.y} x2={cx} y2={BOX.y + BOX.h} stroke={CHART.accent} strokeWidth={1} strokeDasharray="2 3" />
              <text x={cx} y={BOX.y + 9} textAnchor="middle" fill={CHART.accent} fontSize={8}>
                BE {value.toFixed(2)}
              </text>
            </g>
          );
        })}

        <text x={BOX.x} y={250} fill={CHART.axis} fontSize={8.5}>
          {spotLo.toFixed(2)}
        </text>
        <text x={BOX.x + BOX.w / 2} y={250} textAnchor="middle" fill={CHART.axis} fontSize={8.5}>
          spot at expiry
        </text>
        <text x={BOX.x + BOX.w} y={250} textAnchor="end" fill={CHART.axis} fontSize={8.5}>
          {spotHi.toFixed(2)}
        </text>
      </svg>
      <ChartLegend
        items={[
          { label: "Strategy payoff", color: CHART.strong },
          { label: "Profit zone", color: CHART.bullLine },
          { label: "Loss zone", color: CHART.bearLine },
          { label: "Breakeven", color: CHART.accent, dashed: true },
        ]}
      />
    </div>
  );
}
