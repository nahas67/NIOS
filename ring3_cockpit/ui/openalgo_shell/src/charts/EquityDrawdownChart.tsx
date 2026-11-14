/**
 * Strategy NAV versus benchmark with an underwater drawdown pane.
 * Both series share one value domain so the spread is honest; the drawdown pane
 * is always zero-anchored so the deepest trough is proportional.
 */

import type { ReactNode } from "react";
import { num, price } from "@/lib/format";
import { ChartTooltip, GridLines, TooltipRow, VB_W, domainOf, linePath, scale, useCrosshair } from "./kit";

export interface EquityDrawdownChartProps {
  equity: number[];
  benchmark?: number[];
  drawdownPct?: number[];
  height?: number;
  currency?: string;
}

const MARGIN = { left: 62, right: 16, top: 16, bottom: 20 };

export function EquityDrawdownChart({
  equity,
  benchmark,
  drawdownPct,
  height = 320,
  currency = "$",
}: EquityDrawdownChartProps): ReactNode {
  const navH = Math.round((height - MARGIN.top - MARGIN.bottom) * 0.68);
  const ddH = Math.round((height - MARGIN.top - MARGIN.bottom) * 0.26);
  const gap = Math.round((height - MARGIN.top - MARGIN.bottom) * 0.06);
  const plotW = VB_W - MARGIN.left - MARGIN.right;
  const count = equity.length;
  const crosshair = useCrosshair(count);

  if (count === 0) {
    return (
      <div className="empty" style={{ minHeight: height }}>
        <p className="text-[12px] text-[var(--dim)]">Run a backtest to plot the equity and drawdown curves.</p>
      </div>
    );
  }

  const benchmarkClean = benchmark && benchmark.length > 0 ? benchmark : [];
  const navDomain = domainOf([...equity, ...benchmarkClean], 0.05);
  const ddSeries = drawdownPct && drawdownPct.length > 0 ? drawdownPct : [];
  const ddMin = ddSeries.length > 0 ? Math.min(0, ...ddSeries) : -1;
  const ddDomain = { min: ddMin * 1.08, max: 0.6 };

  const xAt = (index: number) => MARGIN.left + (count === 1 ? plotW / 2 : (index / (count - 1)) * plotW);
  const navY = (value: number) => MARGIN.top + navH - scale(value, navDomain, navH);
  const ddTop = MARGIN.top + navH + gap;
  const ddY = (value: number) => ddTop + ddH - scale(value, ddDomain, ddH);

  const equityPath = linePath(equity.map((value, index) => [xAt(index), navY(value)]));
  const benchmarkPath = linePath(benchmarkClean.map((value, index) => [xAt(index), navY(value)]));
  const ddPath =
    ddSeries.length > 0
      ? `M${xAt(0)},${ddTop} ${ddSeries.map((value, index) => `L${xAt(index)},${ddY(value)}`).join(" ")} L${xAt(ddSeries.length - 1)},${ddTop} Z`
      : "";

  const hoveredEquity = crosshair.index !== null ? equity[crosshair.index] : null;
  const hoveredBenchmark = crosshair.index !== null ? benchmarkClean[crosshair.index] : undefined;
  const hoveredDrawdown = crosshair.index !== null && ddSeries.length > 0 ? ddSeries[crosshair.index] : undefined;

  return (
    <div className="relative w-full">
      <svg
        viewBox={`0 0 ${VB_W} ${height}`}
        width="100%"
        height={height}
        role="img"
        aria-label={`Strategy NAV curve over ${count} bars compared with the benchmark, plus the underwater drawdown`}
        {...crosshair.bind}
        style={{ touchAction: "none" }}
      >
        <GridLines
          domain={navDomain}
          plotH={navH}
          plotW={plotW}
          left={MARGIN.left}
          top={MARGIN.top}
          count={4}
          format={(value) => `${currency}${num(value, 0)}`}
        />

        {benchmarkPath ? <path d={benchmarkPath} fill="none" stroke="rgba(148,163,184,0.7)" strokeWidth={1.3} strokeDasharray="5 4" /> : null}
        <path d={equityPath} fill="none" stroke="var(--accent)" strokeWidth={2} style={{ filter: "drop-shadow(0 0 5px var(--accent))" }} />

        <line x1={MARGIN.left} x2={VB_W - MARGIN.right} y1={ddTop - 6} y2={ddTop - 6} stroke="rgba(148,163,184,0.14)" />
        <text x={MARGIN.left - 6} y={ddTop + 8} textAnchor="end" className="iso-ring-label" style={{ fontSize: 8 }}>
          DD
        </text>
        <text x={MARGIN.left + 4} y={ddTop + 9} fill="rgba(251,113,133,0.7)" style={{ fontSize: 8, fontFamily: "var(--font-mono)" }}>
          {num(ddMin, 2)}% worst
        </text>

        <line x1={MARGIN.left} x2={VB_W - MARGIN.right} y1={ddTop} y2={ddTop} stroke="rgba(148,163,184,0.2)" />
        {ddPath ? <path d={ddPath} fill="rgba(251,113,133,0.22)" stroke="rgba(251,113,133,0.65)" strokeWidth={1} /> : null}

        <text x={MARGIN.left} y={height - 5} className="iso-ring-label" style={{ fontSize: 8 }}>
          bar 0
        </text>
        <text x={VB_W - MARGIN.right} y={height - 5} textAnchor="end" className="iso-ring-label" style={{ fontSize: 8 }}>
          bar {count - 1}
        </text>

        {crosshair.index !== null ? (
          <g>
            <line x1={crosshair.x} x2={crosshair.x} y1={MARGIN.top} y2={ddTop + ddH} stroke="rgba(226,232,240,0.26)" strokeDasharray="3 3" />
            {hoveredEquity !== null ? <circle cx={crosshair.x} cy={navY(hoveredEquity)} r={3} fill="var(--accent)" /> : null}
          </g>
        ) : null}
      </svg>

      <div className="pointer-events-none absolute top-2 left-3 flex items-center gap-3">
        <span className="mono flex items-center gap-1 text-[9px] tracking-[0.08em] text-[var(--dim)]">
          <span className="inline-block h-[2px] w-3 rounded bg-[var(--accent)]" /> STRATEGY
        </span>
        <span className="mono flex items-center gap-1 text-[9px] tracking-[0.08em] text-[var(--dim)]">
          <span className="inline-block h-[2px] w-3 rounded bg-[rgba(148,163,184,0.7)]" /> BENCHMARK
        </span>
      </div>

      {hoveredEquity !== null && crosshair.index !== null ? (
        <ChartTooltip x={crosshair.x}>
          <p className="mono mb-1 text-[9px] tracking-[0.1em] text-[var(--dim)] uppercase">bar {crosshair.index}</p>
          <TooltipRow label="strategy" value={`${currency}${price(hoveredEquity)}`} />
          {typeof hoveredBenchmark === "number" ? <TooltipRow label="benchmark" value={`${currency}${price(hoveredBenchmark)}`} /> : null}
          {typeof hoveredDrawdown === "number" ? (
            <TooltipRow label="drawdown" value={`${num(hoveredDrawdown, 2)}%`} tone="text-[var(--neg)]" />
          ) : null}
        </ChartTooltip>
      ) : null}
    </div>
  );
}
