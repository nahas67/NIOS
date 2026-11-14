/**
 * Strategy NAV versus benchmark, with the drawdown row underneath.
 *
 * Both NAV series are re-based to 100 so a freshly funded silo and a matured
 * silo share one axis. Drawdown is drawn as flat burgundy bars, never an area
 * gradient.
 */

import type { ReactNode } from "react";
import { CHART, ChartLegend, extent, linePath, plotGrid, scale } from "./kit";

export interface EquityDrawdownChartProps {
  /** Rebased or raw NAV samples; the chart re-bases to 100 internally. */
  strategy: number[];
  benchmark?: number[];
  drawdownPct?: number[];
  height?: number;
}

const NAV = { x: 52, y: 12, w: 648, h: 176 };
const DD = { x: 52, y: 206, w: 648, h: 48 };

function rebase(series: number[]): number[] {
  const base = series.find((value) => Number.isFinite(value) && value !== 0);
  if (!base) return series.map(() => 100);
  return series.map((value) => (value / base) * 100);
}

export function EquityDrawdownChart({
  strategy,
  benchmark = [],
  drawdownPct = [],
  height = 280,
}: EquityDrawdownChartProps): ReactNode {
  const navSeries = rebase(strategy);
  const benchSeries = rebase(benchmark);

  if (navSeries.length < 2) {
    return (
      <div className="nx-stack">
        <svg className="nx-chart" viewBox="0 0 720 280" style={{ height }} role="img" aria-label="Equity curve empty">
          <title>Equity and drawdown - no observations</title>
          {plotGrid(NAV)}
          {plotGrid(DD, 2, 6)}
          <text x={NAV.x + NAV.w / 2} y={NAV.y + NAV.h / 2} textAnchor="middle" fill={CHART.axis} fontSize={11}>
            NO NAV HISTORY - the first settled mark will seed this curve
          </text>
        </svg>
        <ChartLegend
          items={[
            { label: "Strategy NAV", color: CHART.accent },
            { label: "Benchmark", color: CHART.text, dashed: true },
            { label: "Drawdown", color: CHART.bearFill },
          ]}
        />
      </div>
    );
  }

  const all = [...navSeries, ...benchSeries];
  const [lo, hi] = extent(all, [95, 105]);
  const pad = (hi - lo) * 0.1 || 1;
  const y = scale(lo - pad, hi + pad, NAV.h);
  const xFor = (index: number, length: number) => NAV.x + (NAV.w / Math.max(1, length - 1)) * index;
  const toPoints = (series: number[]) =>
    series.map((value, index) => [xFor(index, series.length), NAV.y + NAV.h - y(value)] as [number, number]);

  const ddValues = drawdownPct.length > 0 ? drawdownPct : navSeries.map(() => 0);
  const ddMax = Math.max(...ddValues.map((value) => Math.abs(value)), 1);
  const barW = Math.max(1, (DD.w / Math.max(1, ddValues.length)) * 0.7);

  return (
    <div className="nx-stack">
      <svg className="nx-chart" viewBox="0 0 720 280" style={{ height }} role="img" aria-label="Strategy equity and drawdown">
        <title>Strategy equity versus benchmark with drawdown</title>
        {plotGrid(NAV)}
        {plotGrid(DD, 2, 6)}
        <line x1={NAV.x} y1={NAV.y + NAV.h} x2={NAV.x + NAV.w} y2={NAV.y + NAV.h} stroke={CHART.grid} strokeWidth={1} />

        {[0, 0.5, 1].map((fraction) => (
          <text
            key={fraction}
            x={NAV.x - 7}
            y={NAV.y + NAV.h * fraction + 3}
            textAnchor="end"
            fill={CHART.axis}
            fontSize={8.5}
          >
            {(hi - (hi - lo) * fraction).toFixed(1)}
          </text>
        ))}

        {benchSeries.length > 1 ? (
          <path d={linePath(toPoints(benchSeries))} fill="none" stroke={CHART.text} strokeWidth={1} strokeDasharray="4 3" />
        ) : null}
        <path d={linePath(toPoints(navSeries))} fill="none" stroke={CHART.accent} strokeWidth={1.5} />

        {ddValues.map((value, index) => {
          const h = (Math.abs(value) / ddMax) * DD.h;
          return (
            <rect
              key={`dd-${index}`}
              x={DD.x + (DD.w / Math.max(1, ddValues.length)) * index}
              y={DD.y + DD.h - h}
              width={barW}
              height={Math.max(1, h)}
              fill={CHART.bearFill}
              stroke={CHART.bearLine}
              strokeWidth={0.5}
            />
          );
        })}
        <text x={DD.x} y={DD.y - 4} fill={CHART.bear} fontSize={8.5}>
          DRAWDOWN max {ddMax.toFixed(2)}%
        </text>
        <text x={NAV.x + NAV.w} y={274} textAnchor="end" fill={CHART.axis} fontSize={8.5}>
          {navSeries.length} observations
        </text>
      </svg>
      <ChartLegend
        items={[
          { label: "Strategy NAV", color: CHART.accent },
          { label: "Benchmark", color: CHART.text, dashed: true },
          { label: "Drawdown", color: CHART.bearLine },
        ]}
      />
    </div>
  );
}
