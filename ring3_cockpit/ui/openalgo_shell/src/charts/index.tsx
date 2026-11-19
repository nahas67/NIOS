/**
 * Eight flat SVG chart primitives.
 *
 * Every chart is a pure function of its props: no gradients, no shadows, no
 * filters, 1.5px hairlines and flat translucent fills only.  Charts scale with
 * `vector-effect="non-scaling-stroke"` so a 6-inch monitor and a 34-inch
 * monitor show identical stroke weights.
 */

import type { ReactNode } from "react";

export const CHART_COLORS = {
  accent: "#F96316",
  bull: "#38B27B",
  bear: "#E05555",
  ink: "#F4F5F7",
  ink2: "#8C9099",
  ink3: "#5A5E66",
  hair: "#27292E",
  surface: "#16181C",
  elevated: "#22242A",
  canvas: "#0D0E10",
} as const;

function extent(values: number[]): { min: number; max: number } {
  const finite = values.filter((value) => Number.isFinite(value));
  if (finite.length === 0) return { min: 0, max: 1 };
  let min = finite[0];
  let max = finite[0];
  for (const value of finite) {
    if (value < min) min = value;
    if (value > max) max = value;
  }
  if (min === max) {
    const pad = Math.abs(min) * 0.05 || 1;
    return { min: min - pad, max: max + pad };
  }
  return { min, max };
}

function clamp01(value: number): number {
  return Math.max(0, Math.min(1, value));
}

function polyline(points: number[], width: number, height: number, pad = 4): string {
  const clean = points.filter((point) => Number.isFinite(point));
  if (clean.length === 0) return "";
  const { min, max } = extent(clean);
  const span = max - min || 1;
  const innerW = width - pad * 2;
  const innerH = height - pad * 2;
  const step = clean.length > 1 ? innerW / (clean.length - 1) : 0;
  return clean
    .map((point, index) => {
      const x = pad + index * step;
      const y = pad + (1 - (point - min) / span) * innerH;
      return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
}

function EmptyPlot({ width = 640, height = 160, label = "NO DATA" }: { width?: number; height?: number; label?: string }) {
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label={label}>
      <rect x="0.5" y="0.5" width={width - 1} height={height - 1} fill="none" stroke={CHART_COLORS.hair} />
      <text x={width / 2} y={height / 2} textAnchor="middle" fill={CHART_COLORS.ink3} fontSize="10" fontFamily="inherit">
        {label}
      </text>
    </svg>
  );
}

export interface Series {
  name: string;
  points: number[];
  color?: string;
}

export function LineChart({ series, height = 180, labels, baseline, width = 640 }: { series: Series[]; height?: number; labels?: string[]; baseline?: number; width?: number }) {
  const all = series.flatMap((entry) => entry.points);
  if (all.length === 0) return <EmptyPlot width={width} height={height} />;
  const { min, max } = extent(baseline === undefined ? all : [...all, baseline]);
  const span = max - min || 1;
  const pad = 6;
  const yFor = (value: number) => pad + (1 - (value - min) / span) * (height - pad * 2);

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label="line chart">
      {[0.25, 0.5, 0.75].map((ratio) => (
        <line key={ratio} x1={0} x2={width} y1={height * ratio} y2={height * ratio} stroke={CHART_COLORS.hair} strokeWidth="1" />
      ))}
      {baseline !== undefined ? (
        <line x1={0} x2={width} y1={yFor(baseline)} y2={yFor(baseline)} stroke={CHART_COLORS.ink3} strokeWidth="1" strokeDasharray="3 3" />
      ) : null}
      {series.map((entry, index) => (
        <path
          key={`${entry.name}-${index}`}
          d={polyline(entry.points, width, height, pad)}
          fill="none"
          stroke={entry.color ?? [CHART_COLORS.accent, CHART_COLORS.bull, CHART_COLORS.bear, CHART_COLORS.ink2][index % 4]}
          strokeWidth="1.5"
          strokeLinejoin="round"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />
      ))}
      {labels && labels.length > 0 ? (
        <text x={width - pad} y={height - 2} textAnchor="end" fill={CHART_COLORS.ink3} fontSize="9">
          {labels[labels.length - 1]}
        </text>
      ) : null}
    </svg>
  );
}

export function Sparkline({ points, tone = "accent", height = 28, width = 120 }: { points: number[]; tone?: "accent" | "bull" | "bear" | "muted"; height?: number; width?: number }) {
  if (points.length === 0) return <svg viewBox={`0 0 ${width} ${height}`} className="w-full" aria-hidden="true" />;
  const color = tone === "bull" ? CHART_COLORS.bull : tone === "bear" ? CHART_COLORS.bear : tone === "muted" ? CHART_COLORS.ink3 : CHART_COLORS.accent;
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label="sparkline" preserveAspectRatio="none">
      <path d={polyline(points, width, height, 2)} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export interface BarDatum {
  label: string;
  value: number;
  tone?: "accent" | "bull" | "bear" | "muted";
}

export function BarChart({ bars, height = 180, width = 640, horizontal = false, format }: { bars: BarDatum[]; height?: number; width?: number; horizontal?: boolean; format?: (value: number) => string }) {
  if (bars.length === 0) return <EmptyPlot width={width} height={height} />;
  const values = bars.map((bar) => bar.value);
  const { min, max } = extent(values);
  const span = Math.max(Math.abs(min), Math.abs(max)) || 1;
  const colorFor = (tone?: BarDatum["tone"]) =>
    tone === "bull" ? CHART_COLORS.bull : tone === "bear" ? CHART_COLORS.bear : tone === "muted" ? CHART_COLORS.ink3 : CHART_COLORS.accent;

  if (horizontal) {
    const rowHeight = Math.max(14, Math.min(26, (height - 4) / bars.length));
    return (
      <svg viewBox={`0 0 ${width} ${bars.length * rowHeight + 4}`} className="w-full" role="img" aria-label="bar chart">
        {bars.map((bar, index) => {
          const zeroX = width * 0.42;
          const usable = width * 0.5;
          const length = (Math.abs(bar.value) / span) * usable;
          const x = bar.value >= 0 ? zeroX : zeroX - length;
          const y = index * rowHeight + 2;
          return (
            <g key={`${bar.label}-${index}`}>
              <text x={zeroX - 6} y={y + rowHeight * 0.66} textAnchor="end" fill={CHART_COLORS.ink2} fontSize="9">
                {bar.label}
              </text>
              <rect x={x} y={y + rowHeight * 0.2} width={Math.max(1, length)} height={rowHeight * 0.55} fill={colorFor(bar.tone)} opacity="0.85" />
              <text x={zeroX + usable + 4} y={y + rowHeight * 0.66} fill={CHART_COLORS.ink3} fontSize="9">
                {format ? format(bar.value) : bar.value.toFixed(2)}
              </text>
            </g>
          );
        })}
        <line x1={width * 0.42} x2={width * 0.42} y1={0} y2={bars.length * rowHeight + 4} stroke={CHART_COLORS.hair} strokeWidth="1" />
      </svg>
    );
  }

  const columnWidth = width / bars.length;
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label="bar chart">
      <line x1={0} x2={width} y1={height - 14} y2={height - 14} stroke={CHART_COLORS.hair} strokeWidth="1" />
      {bars.map((bar, index) => {
        const barHeight = (Math.abs(bar.value) / span) * (height - 24);
        const x = index * columnWidth + columnWidth * 0.2;
        const y = bar.value >= 0 ? height - 14 - barHeight : height - 14;
        return (
          <g key={`${bar.label}-${index}`}>
            <rect x={x} y={y} width={columnWidth * 0.6} height={Math.max(1, barHeight)} fill={colorFor(bar.tone)} opacity="0.85" />
            <text x={x + columnWidth * 0.3} y={height - 3} textAnchor="middle" fill={CHART_COLORS.ink3} fontSize="8.5">
              {bar.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

export interface Candle {
  t: number | string;
  o: number;
  h: number;
  l: number;
  c: number;
  v?: number;
}

export interface CandleOverlays {
  ma5?: number[];
  ma10?: number[];
  ma20?: number[];
  vwap?: number[];
}

export function CandlestickChart({ candles, overlays, height = 300, width = 720 }: { candles: Candle[]; overlays?: CandleOverlays; height?: number; width?: number }) {
  if (candles.length === 0) return <EmptyPlot width={width} height={height} label="AWAITING BARS" />;
  const lows = candles.map((candle) => candle.l);
  const highs = candles.map((candle) => candle.h);
  const min = Math.min(...lows.filter(Number.isFinite));
  const max = Math.max(...highs.filter(Number.isFinite));
  const span = max - min || 1;
  const pad = 8;
  const innerH = height - pad * 2;
  const step = (width - pad * 2) / candles.length;
  const bodyWidth = Math.max(1.5, step * 0.55);
  const yFor = (value: number) => pad + (1 - (value - min) / span) * innerH;

  const overlayPath = (points: number[] | undefined) => {
    if (!points || points.length === 0) return "";
    return points
      .map((point, index) => {
        if (!Number.isFinite(point)) return "";
        const x = pad + index * step + step / 2;
        return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${yFor(point).toFixed(2)}`;
      })
      .join(" ")
      .replace(/^L/, "M");
  };

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label="candlestick chart">
      {[0.2, 0.4, 0.6, 0.8].map((ratio) => (
        <line key={ratio} x1={0} x2={width} y1={height * ratio} y2={height * ratio} stroke={CHART_COLORS.hair} strokeWidth="1" />
      ))}
      {candles.map((candle, index) => {
        const x = pad + index * step + step / 2;
        const up = candle.c >= candle.o;
        const color = up ? CHART_COLORS.bull : CHART_COLORS.bear;
        const top = yFor(Math.max(candle.o, candle.c));
        const bottom = yFor(Math.min(candle.o, candle.c));
        return (
          <g key={`${candle.t}-${index}`}>
            <line x1={x} x2={x} y1={yFor(candle.h)} y2={yFor(candle.l)} stroke={color} strokeWidth="1" opacity="0.8" />
            <rect x={x - bodyWidth / 2} y={top} width={bodyWidth} height={Math.max(1, bottom - top)} fill={color} opacity={up ? 0.85 : 0.9} />
          </g>
        );
      })}
      {overlays?.ma5 ? <path d={overlayPath(overlays.ma5)} fill="none" stroke={CHART_COLORS.accent} strokeWidth="1.2" vectorEffect="non-scaling-stroke" /> : null}
      {overlays?.ma10 ? <path d={overlayPath(overlays.ma10)} fill="none" stroke={CHART_COLORS.ink2} strokeWidth="1.2" vectorEffect="non-scaling-stroke" /> : null}
      {overlays?.ma20 ? <path d={overlayPath(overlays.ma20)} fill="none" stroke={CHART_COLORS.bear} strokeWidth="1.2" vectorEffect="non-scaling-stroke" /> : null}
      {overlays?.vwap ? (
        <path d={overlayPath(overlays.vwap)} fill="none" stroke={CHART_COLORS.ink} strokeWidth="1" strokeDasharray="4 3" vectorEffect="non-scaling-stroke" opacity="0.7" />
      ) : null}
    </svg>
  );
}

export interface DepthLevel {
  price: number;
  size: number;
}

export function DepthLadder({ bids, asks, mid, tickSizeMicros, height = 260, width = 320 }: { bids: DepthLevel[]; asks: DepthLevel[]; mid?: number; tickSizeMicros?: number; height?: number; width?: number }) {
  const rows = Math.max(bids.length, asks.length, 10);
  if (rows === 0) return <EmptyPlot width={width} height={height} label="NO DEPTH" />;
  const rowHeight = (height - 18) / rows;
  const maxSize = Math.max(1, ...bids.map((level) => level.size), ...asks.map((level) => level.size));

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label="level 2 depth ladder">
      <text x={4} y={9} fill={CHART_COLORS.ink3} fontSize="8">
        BID
      </text>
      <text x={width - 4} y={9} textAnchor="end" fill={CHART_COLORS.ink3} fontSize="8">
        ASK
      </text>
      {bids.slice(0, rows).map((level, index) => {
        const barWidth = (level.size / maxSize) * (width / 2 - 10);
        const y = 12 + index * rowHeight;
        return (
          <g key={`bid-${index}`}>
            <rect x={width / 2 - 4 - barWidth} y={y} width={Math.max(1, barWidth)} height={Math.max(1, rowHeight - 1)} fill={CHART_COLORS.bull} opacity="0.18" />
            <text x={4} y={y + rowHeight * 0.72} fill={CHART_COLORS.bull} fontSize="8.5">
              {level.price.toFixed(2)}
            </text>
            <text x={width / 2 - 8} y={y + rowHeight * 0.72} textAnchor="end" fill={CHART_COLORS.ink3} fontSize="8">
              {level.size.toFixed(0)}
            </text>
          </g>
        );
      })}
      {asks.slice(0, rows).map((level, index) => {
        const barWidth = (level.size / maxSize) * (width / 2 - 10);
        const y = 12 + index * rowHeight;
        return (
          <g key={`ask-${index}`}>
            <rect x={width / 2 + 4} y={y} width={Math.max(1, barWidth)} height={Math.max(1, rowHeight - 1)} fill={CHART_COLORS.bear} opacity="0.18" />
            <text x={width - 4} y={y + rowHeight * 0.72} textAnchor="end" fill={CHART_COLORS.bear} fontSize="8.5">
              {level.price.toFixed(2)}
            </text>
            <text x={width / 2 + 8} y={y + rowHeight * 0.72} fill={CHART_COLORS.ink3} fontSize="8">
              {level.size.toFixed(0)}
            </text>
          </g>
        );
      })}
      <line x1={width / 2} x2={width / 2} y1={10} y2={height} stroke={CHART_COLORS.hair} strokeWidth="1" />
      {mid !== undefined && Number.isFinite(mid) ? (
        <text x={width / 2} y={height - 2} textAnchor="middle" fill={CHART_COLORS.ink2} fontSize="8.5">
          MID {mid.toFixed(2)}
          {tickSizeMicros ? ` · TICK ${(tickSizeMicros / 1_000_000).toFixed(2)}` : ""}
        </text>
      ) : null}
    </svg>
  );
}

export interface PayoffPoint {
  spot: number;
  pnl: number;
}

export function PayoffCurveChart({ points, breakevens, height = 200, width = 640 }: { points: PayoffPoint[]; breakevens?: number[]; height?: number; width?: number }) {
  if (points.length < 2) return <EmptyPlot width={width} height={height} label="BUILD A STRATEGY" />;
  const spots = points.map((point) => point.spot);
  const pnls = points.map((point) => point.pnl);
  const xMin = Math.min(...spots);
  const xMax = Math.max(...spots);
  const yExtent = extent([...pnls, 0]);
  const pad = 8;
  const xFor = (spot: number) => pad + ((spot - xMin) / (xMax - xMin || 1)) * (width - pad * 2);
  const yFor = (pnl: number) => pad + (1 - (pnl - yExtent.min) / (yExtent.max - yExtent.min || 1)) * (height - pad * 2);

  const positive: string[] = [];
  const negative: string[] = [];
  let previous = points[0];
  for (const point of points) {
    const segment = `M${xFor(previous.spot).toFixed(2)},${yFor(previous.pnl).toFixed(2)} L${xFor(point.spot).toFixed(2)},${yFor(point.pnl).toFixed(2)}`;
    if ((previous.pnl + point.pnl) / 2 >= 0) positive.push(segment);
    else negative.push(segment);
    previous = point;
  }

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label="payoff curve">
      <line x1={0} x2={width} y1={yFor(0)} y2={yFor(0)} stroke={CHART_COLORS.hair} strokeWidth="1" />
      {positive.map((segment, index) => (
        <path key={`pos-${index}`} d={segment} fill="none" stroke={CHART_COLORS.bull} strokeWidth="1.6" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
      ))}
      {negative.map((segment, index) => (
        <path key={`neg-${index}`} d={segment} fill="none" stroke={CHART_COLORS.bear} strokeWidth="1.6" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
      ))}
      {(breakevens ?? []).map((spot, index) => (
        <g key={`be-${index}`}>
          <line x1={xFor(spot)} x2={xFor(spot)} y1={pad} y2={height - pad} stroke={CHART_COLORS.accent} strokeWidth="1" strokeDasharray="2 3" />
          <text x={xFor(spot)} y={pad + 8} textAnchor="middle" fill={CHART_COLORS.accent} fontSize="8">
            BE {spot.toFixed(2)}
          </text>
        </g>
      ))}
    </svg>
  );
}

export interface HeatCell {
  label: string;
  value: number;
  sub?: string;
}

function heatColor(value: number, scale: number): string {
  const ratio = clamp01(Math.abs(value) / (scale || 1));
  const base = value >= 0 ? "56,178,123" : "224,85,85";
  return `rgba(${base},${(0.08 + ratio * 0.62).toFixed(3)})`;
}

export function Heatmap({ cells, columns = 6, width = 640, cellHeight = 46 }: { cells: HeatCell[]; columns?: number; width?: number; cellHeight?: number }) {
  if (cells.length === 0) return <EmptyPlot width={width} height={120} label="NO HEATMAP" />;
  const rows = Math.ceil(cells.length / columns);
  const cellWidth = width / columns;
  const height = rows * cellHeight;
  const scale = Math.max(1, ...cells.map((cell) => Math.abs(cell.value)));
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label="heatmap">
      {cells.map((cell, index) => {
        const x = (index % columns) * cellWidth;
        const y = Math.floor(index / columns) * cellHeight;
        return (
          <g key={`${cell.label}-${index}`}>
            <rect x={x + 1} y={y + 1} width={cellWidth - 2} height={cellHeight - 2} fill={heatColor(cell.value, scale)} stroke={CHART_COLORS.hair} strokeWidth="1" />
            <text x={x + 6} y={y + 17} fill={CHART_COLORS.ink} fontSize="9.5" fontWeight="600">
              {cell.label}
            </text>
            <text x={x + 6} y={y + 31} fill={cell.value >= 0 ? CHART_COLORS.bull : CHART_COLORS.bear} fontSize="9">
              {cell.value >= 0 ? "+" : ""}
              {cell.value.toFixed(2)}%
            </text>
            {cell.sub ? (
              <text x={x + cellWidth - 6} y={y + cellHeight - 8} textAnchor="end" fill={CHART_COLORS.ink3} fontSize="8">
                {cell.sub}
              </text>
            ) : null}
          </g>
        );
      })}
    </svg>
  );
}

export function EquityDrawdownCurve({ equity, drawdown, height = 220, width = 640 }: { equity: number[]; drawdown: number[]; height?: number; width?: number }) {
  if (equity.length === 0) return <EmptyPlot width={width} height={height} label="NO EQUITY CURVE" />;
  const topHeight = height * 0.66;
  const bottomTop = topHeight + 8;
  const bottomHeight = height - bottomTop;
  const { min, max } = extent(equity);
  const span = max - min || 1;
  const pad = 4;
  const step = equity.length > 1 ? (width - pad * 2) / (equity.length - 1) : 0;
  const equityPath = equity
    .map((value, index) => `${index === 0 ? "M" : "L"}${(pad + index * step).toFixed(2)},${(pad + (1 - (value - min) / span) * (topHeight - pad * 2)).toFixed(2)}`)
    .join(" ");
  const maxDrawdown = Math.max(1, ...drawdown.map((value) => Math.abs(value)));
  const barWidth = Math.max(1, step * 0.8);

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label="equity and drawdown curve">
      <line x1={0} x2={width} y1={topHeight} y2={topHeight} stroke={CHART_COLORS.hair} strokeWidth="1" />
      <path d={`${equityPath} L${width - pad},${topHeight - pad} L${pad},${topHeight - pad} Z`} fill={CHART_COLORS.accent} opacity="0.07" />
      <path d={equityPath} fill="none" stroke={CHART_COLORS.accent} strokeWidth="1.5" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
      {drawdown.map((value, index) => {
        const magnitude = (Math.abs(value) / maxDrawdown) * (bottomHeight - 2);
        return (
          <rect
            key={index}
            x={pad + index * step}
            y={bottomTop}
            width={barWidth}
            height={Math.max(1, magnitude)}
            fill={CHART_COLORS.bear}
            opacity="0.5"
          />
        );
      })}
      <text x={pad} y={height - 1} fill={CHART_COLORS.ink3} fontSize="8">
        UNDERWATER
      </text>
      <text x={width - pad} y={height - 1} textAnchor="end" fill={CHART_COLORS.ink3} fontSize="8">
        MAX DD {maxDrawdown.toFixed(2)}%
      </text>
    </svg>
  );
}

export function ChartLegend({ items }: { items: Array<{ label: string; color: string }> }): ReactNode {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      {items.map((item) => (
        <span key={item.label} className="inline-flex items-center gap-1 text-[10px] uppercase tracking-[0.08em] text-ink-3">
          <span className="inline-block h-[2px] w-3" style={{ backgroundColor: item.color }} />
          {item.label}
        </span>
      ))}
    </div>
  );
}
