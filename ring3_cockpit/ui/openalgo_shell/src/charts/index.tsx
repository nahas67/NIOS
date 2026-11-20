/**
 * Eight flat SVG chart primitives.
 *
 * Every chart is a pure function of its props: no gradients, no shadows, no
 * filters, 1.5px hairlines and flat translucent fills only.  Charts scale with
 * `vector-effect="non-scaling-stroke"` so a 6-inch monitor and a 34-inch
 * monitor show identical stroke weights.
 *
 * Each chart is memoised on its props and memoises its derived geometry, so an
 * unrelated parent re-render (a session TTL ticker, a polled blotter) never
 * rebuilds an SVG path string or re-emits identical elements.
 */

import { memo, useMemo } from "react";
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

const SERIES_STROKES = [CHART_COLORS.accent, CHART_COLORS.bull, CHART_COLORS.bear, CHART_COLORS.ink2];

export interface LineChartProps {
  series: Series[];
  height?: number;
  labels?: string[];
  baseline?: number;
  width?: number;
}

function LineChartBase({ series, height = 180, labels, baseline, width = 640 }: LineChartProps) {
  const plot = useMemo(() => {
    const all = series.flatMap((entry) => entry.points);
    if (all.length === 0) return null;
    const { min, max } = extent(baseline === undefined ? all : [...all, baseline]);
    const span = max - min || 1;
    const pad = 6;
    const yFor = (value: number) => pad + (1 - (value - min) / span) * (height - pad * 2);
    return {
      pad,
      baselineY: baseline === undefined ? null : yFor(baseline),
      paths: series.map((entry, index) => ({
        key: `${entry.name}-${index}`,
        d: polyline(entry.points, width, height, pad),
        stroke: entry.color ?? SERIES_STROKES[index % 4],
      })),
    };
  }, [series, height, width, baseline]);

  if (plot === null) return <EmptyPlot width={width} height={height} />;

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label="line chart">
      {[0.25, 0.5, 0.75].map((ratio) => (
        <line key={ratio} x1={0} x2={width} y1={height * ratio} y2={height * ratio} stroke={CHART_COLORS.hair} strokeWidth="1" />
      ))}
      {plot.baselineY !== null ? (
        <line
          x1={0}
          x2={width}
          y1={plot.baselineY}
          y2={plot.baselineY}
          stroke={CHART_COLORS.ink3}
          strokeWidth="1"
          strokeDasharray="3 3"
        />
      ) : null}
      {plot.paths.map((path) => (
        <path
          key={path.key}
          d={path.d}
          fill="none"
          stroke={path.stroke}
          strokeWidth="1.5"
          strokeLinejoin="round"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />
      ))}
      {labels && labels.length > 0 ? (
        <text x={width - plot.pad} y={height - 2} textAnchor="end" fill={CHART_COLORS.ink3} fontSize="9">
          {labels[labels.length - 1]}
        </text>
      ) : null}
    </svg>
  );
}

export const LineChart = memo(LineChartBase);
LineChart.displayName = "LineChart";

export interface SparklineProps {
  points: number[];
  tone?: "accent" | "bull" | "bear" | "muted";
  height?: number;
  width?: number;
}

function sparkStroke(tone: SparklineProps["tone"]): string {
  return tone === "bull"
    ? CHART_COLORS.bull
    : tone === "bear"
      ? CHART_COLORS.bear
      : tone === "muted"
        ? CHART_COLORS.ink3
        : CHART_COLORS.accent;
}

function SparklineBase({ points, tone = "accent", height = 28, width = 120 }: SparklineProps) {
  const path = useMemo(
    () => (points.length === 0 ? null : polyline(points, width, height, 2)),
    [points, width, height],
  );
  const color = sparkStroke(tone);
  if (path === null) return <svg viewBox={`0 0 ${width} ${height}`} className="w-full" aria-hidden="true" />;
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label="sparkline" preserveAspectRatio="none">
      <path d={path} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export const Sparkline = memo(SparklineBase);
Sparkline.displayName = "Sparkline";

export interface BarDatum {
  label: string;
  value: number;
  tone?: "accent" | "bull" | "bear" | "muted";
}

export interface BarChartProps {
  bars: BarDatum[];
  height?: number;
  width?: number;
  horizontal?: boolean;
  format?: (value: number) => string;
}

function barToneColor(tone?: BarDatum["tone"]): string {
  return tone === "bull"
    ? CHART_COLORS.bull
    : tone === "bear"
      ? CHART_COLORS.bear
      : tone === "muted"
        ? CHART_COLORS.ink3
        : CHART_COLORS.accent;
}

function BarChartBase({ bars, height = 180, width = 640, horizontal = false, format }: BarChartProps) {
  const layout = useMemo(() => {
    if (bars.length === 0) return null;
    const values = bars.map((bar) => bar.value);
    const { min, max } = extent(values);
    const span = Math.max(Math.abs(min), Math.abs(max)) || 1;
    const rowHeight = Math.max(14, Math.min(26, (height - 4) / bars.length));
    const columnWidth = width / bars.length;
    const rows = bars.map((bar, index) => {
      const zeroX = width * 0.42;
      const usable = width * 0.5;
      const length = (Math.abs(bar.value) / span) * usable;
      return {
        key: `${bar.label}-${index}`,
        label: bar.label,
        value: bar.value,
        fill: barToneColor(bar.tone),
        text: bar.value.toFixed(2),
        x: bar.value >= 0 ? zeroX : zeroX - length,
        y: index * rowHeight + 2,
        width: Math.max(1, length),
      };
    });
    const columns = bars.map((bar, index) => {
      const barHeight = (Math.abs(bar.value) / span) * (height - 24);
      return {
        key: `${bar.label}-${index}`,
        label: bar.label,
        fill: barToneColor(bar.tone),
        x: index * columnWidth + columnWidth * 0.2,
        y: bar.value >= 0 ? height - 14 - barHeight : height - 14,
        width: columnWidth * 0.6,
        height: Math.max(1, barHeight),
      };
    });
    return { rowHeight, columnWidth, rows, columns };
  }, [bars, height, width]);

  if (layout === null) return <EmptyPlot width={width} height={height} />;

  if (horizontal) {
    return (
      <svg viewBox={`0 0 ${width} ${bars.length * layout.rowHeight + 4}`} className="w-full" role="img" aria-label="bar chart">
        {layout.rows.map((row) => (
          <g key={row.key}>
            <text x={width * 0.42 - 6} y={row.y + layout.rowHeight * 0.66} textAnchor="end" fill={CHART_COLORS.ink2} fontSize="9">
              {row.label}
            </text>
            <rect
              x={row.x}
              y={row.y + layout.rowHeight * 0.2}
              width={row.width}
              height={layout.rowHeight * 0.55}
              fill={row.fill}
              opacity="0.85"
            />
            <text x={width * 0.42 + width * 0.5 + 4} y={row.y + layout.rowHeight * 0.66} fill={CHART_COLORS.ink3} fontSize="9">
              {format ? format(row.value) : row.text}
            </text>
          </g>
        ))}
        <line
          x1={width * 0.42}
          x2={width * 0.42}
          y1={0}
          y2={bars.length * layout.rowHeight + 4}
          stroke={CHART_COLORS.hair}
          strokeWidth="1"
        />
      </svg>
    );
  }

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label="bar chart">
      <line x1={0} x2={width} y1={height - 14} y2={height - 14} stroke={CHART_COLORS.hair} strokeWidth="1" />
      {layout.columns.map((column) => (
        <g key={column.key}>
          <rect x={column.x} y={column.y} width={column.width} height={column.height} fill={column.fill} opacity="0.85" />
          <text
            x={column.x + layout.columnWidth * 0.3}
            y={height - 3}
            textAnchor="middle"
            fill={CHART_COLORS.ink3}
            fontSize="8.5"
          >
            {column.label}
          </text>
        </g>
      ))}
    </svg>
  );
}

export const BarChart = memo(BarChartBase);
BarChart.displayName = "BarChart";

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

export interface CandlestickChartProps {
  candles: Candle[];
  overlays?: CandleOverlays;
  height?: number;
  width?: number;
}

function CandlestickChartBase({ candles, overlays, height = 300, width = 720 }: CandlestickChartProps) {
  const plot = useMemo(() => {
    if (candles.length === 0) return null;
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

    return {
      bodyWidth,
      bars: candles.map((candle, index) => {
        const x = pad + index * step + step / 2;
        const up = candle.c >= candle.o;
        const top = yFor(Math.max(candle.o, candle.c));
        const bottom = yFor(Math.min(candle.o, candle.c));
        return {
          key: `${candle.t}-${index}`,
          x,
          wickTop: yFor(candle.h),
          wickBottom: yFor(candle.l),
          bodyY: top,
          bodyHeight: Math.max(1, bottom - top),
          fill: up ? CHART_COLORS.bull : CHART_COLORS.bear,
          opacity: up ? 0.85 : 0.9,
        };
      }),
      ma5: overlayPath(overlays?.ma5),
      ma10: overlayPath(overlays?.ma10),
      ma20: overlayPath(overlays?.ma20),
      vwap: overlayPath(overlays?.vwap),
    };
  }, [candles, overlays, height, width]);

  if (plot === null) return <EmptyPlot width={width} height={height} label="AWAITING BARS" />;

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label="candlestick chart">
      {[0.2, 0.4, 0.6, 0.8].map((ratio) => (
        <line key={ratio} x1={0} x2={width} y1={height * ratio} y2={height * ratio} stroke={CHART_COLORS.hair} strokeWidth="1" />
      ))}
      {plot.bars.map((bar) => (
        <g key={bar.key}>
          <line x1={bar.x} x2={bar.x} y1={bar.wickTop} y2={bar.wickBottom} stroke={bar.fill} strokeWidth="1" opacity="0.8" />
          <rect
            x={bar.x - plot.bodyWidth / 2}
            y={bar.bodyY}
            width={plot.bodyWidth}
            height={bar.bodyHeight}
            fill={bar.fill}
            opacity={bar.opacity}
          />
        </g>
      ))}
      {overlays?.ma5 ? (
        <path d={plot.ma5} fill="none" stroke={CHART_COLORS.accent} strokeWidth="1.2" vectorEffect="non-scaling-stroke" />
      ) : null}
      {overlays?.ma10 ? (
        <path d={plot.ma10} fill="none" stroke={CHART_COLORS.ink2} strokeWidth="1.2" vectorEffect="non-scaling-stroke" />
      ) : null}
      {overlays?.ma20 ? (
        <path d={plot.ma20} fill="none" stroke={CHART_COLORS.bear} strokeWidth="1.2" vectorEffect="non-scaling-stroke" />
      ) : null}
      {overlays?.vwap ? (
        <path
          d={plot.vwap}
          fill="none"
          stroke={CHART_COLORS.ink}
          strokeWidth="1"
          strokeDasharray="4 3"
          vectorEffect="non-scaling-stroke"
          opacity="0.7"
        />
      ) : null}
    </svg>
  );
}

export const CandlestickChart = memo(CandlestickChartBase);
CandlestickChart.displayName = "CandlestickChart";

export interface DepthLevel {
  price: number;
  size: number;
}

export interface DepthLadderProps {
  bids: DepthLevel[];
  asks: DepthLevel[];
  mid?: number;
  tickSizeMicros?: number;
  height?: number;
  width?: number;
}

function DepthLadderBase({ bids, asks, mid, tickSizeMicros, height = 260, width = 320 }: DepthLadderProps) {
  const plot = useMemo(() => {
    const rows = Math.max(bids.length, asks.length, 10);
    if (rows === 0) return null;
    const rowHeight = (height - 18) / rows;
    const maxSize = Math.max(1, ...bids.map((level) => level.size), ...asks.map((level) => level.size));
    const bidLevels = bids.slice(0, rows).map((level, index) => {
      const barWidth = (level.size / maxSize) * (width / 2 - 10);
      return {
        key: `bid-${index}`,
        x: width / 2 - 4 - barWidth,
        y: 12 + index * rowHeight,
        barWidth: Math.max(1, barWidth),
        price: level.price.toFixed(2),
        size: level.size.toFixed(0),
      };
    });
    const askLevels = asks.slice(0, rows).map((level, index) => {
      const barWidth = (level.size / maxSize) * (width / 2 - 10);
      return {
        key: `ask-${index}`,
        x: width / 2 + 4,
        y: 12 + index * rowHeight,
        barWidth: Math.max(1, barWidth),
        price: level.price.toFixed(2),
        size: level.size.toFixed(0),
      };
    });
    return {
      rowHeight,
      bidLevels,
      askLevels,
      midText: mid !== undefined && Number.isFinite(mid) ? mid.toFixed(2) : null,
      tickText: tickSizeMicros ? (tickSizeMicros / 1_000_000).toFixed(2) : "",
    };
  }, [bids, asks, mid, tickSizeMicros, height, width]);

  if (plot === null) return <EmptyPlot width={width} height={height} label="NO DEPTH" />;

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label="level 2 depth ladder">
      <text x={4} y={9} fill={CHART_COLORS.ink3} fontSize="8">
        BID
      </text>
      <text x={width - 4} y={9} textAnchor="end" fill={CHART_COLORS.ink3} fontSize="8">
        ASK
      </text>
      {plot.bidLevels.map((level) => (
        <g key={level.key}>
          <rect
            x={level.x}
            y={level.y}
            width={level.barWidth}
            height={Math.max(1, plot.rowHeight - 1)}
            fill={CHART_COLORS.bull}
            opacity="0.18"
          />
          <text x={4} y={level.y + plot.rowHeight * 0.72} fill={CHART_COLORS.bull} fontSize="8.5">
            {level.price}
          </text>
          <text x={width / 2 - 8} y={level.y + plot.rowHeight * 0.72} textAnchor="end" fill={CHART_COLORS.ink3} fontSize="8">
            {level.size}
          </text>
        </g>
      ))}
      {plot.askLevels.map((level) => (
        <g key={level.key}>
          <rect
            x={level.x}
            y={level.y}
            width={level.barWidth}
            height={Math.max(1, plot.rowHeight - 1)}
            fill={CHART_COLORS.bear}
            opacity="0.18"
          />
          <text x={width - 4} y={level.y + plot.rowHeight * 0.72} textAnchor="end" fill={CHART_COLORS.bear} fontSize="8.5">
            {level.price}
          </text>
          <text x={width / 2 + 8} y={level.y + plot.rowHeight * 0.72} fill={CHART_COLORS.ink3} fontSize="8">
            {level.size}
          </text>
        </g>
      ))}
      <line x1={width / 2} x2={width / 2} y1={10} y2={height} stroke={CHART_COLORS.hair} strokeWidth="1" />
      {plot.midText !== null ? (
        <text x={width / 2} y={height - 2} textAnchor="middle" fill={CHART_COLORS.ink2} fontSize="8.5">
          MID {plot.midText}
          {plot.tickText ? ` · TICK ${plot.tickText}` : ""}
        </text>
      ) : null}
    </svg>
  );
}

export const DepthLadder = memo(DepthLadderBase);
DepthLadder.displayName = "DepthLadder";

export interface PayoffPoint {
  spot: number;
  pnl: number;
}

export interface PayoffCurveChartProps {
  points: PayoffPoint[];
  breakevens?: number[];
  height?: number;
  width?: number;
}

function PayoffCurveChartBase({ points, breakevens, height = 200, width = 640 }: PayoffCurveChartProps) {
  const plot = useMemo(() => {
    if (points.length < 2) return null;
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

    return {
      pad,
      zeroY: yFor(0),
      positive,
      negative,
      marks: (breakevens ?? []).map((spot, index) => {
        const x = xFor(spot);
        return { key: `be-${index}`, x, spot };
      }),
    };
  }, [points, breakevens, height, width]);

  if (plot === null) return <EmptyPlot width={width} height={height} label="BUILD A STRATEGY" />;

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label="payoff curve">
      <line x1={0} x2={width} y1={plot.zeroY} y2={plot.zeroY} stroke={CHART_COLORS.hair} strokeWidth="1" />
      {plot.positive.map((segment, index) => (
        <path
          key={`pos-${index}`}
          d={segment}
          fill="none"
          stroke={CHART_COLORS.bull}
          strokeWidth="1.6"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
      ))}
      {plot.negative.map((segment, index) => (
        <path
          key={`neg-${index}`}
          d={segment}
          fill="none"
          stroke={CHART_COLORS.bear}
          strokeWidth="1.6"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
      ))}
      {plot.marks.map((mark) => (
        <g key={mark.key}>
          <line
            x1={mark.x}
            x2={mark.x}
            y1={plot.pad}
            y2={height - plot.pad}
            stroke={CHART_COLORS.accent}
            strokeWidth="1"
            strokeDasharray="2 3"
          />
          <text x={mark.x} y={plot.pad + 8} textAnchor="middle" fill={CHART_COLORS.accent} fontSize="8">
            BE {mark.spot.toFixed(2)}
          </text>
        </g>
      ))}
    </svg>
  );
}

export const PayoffCurveChart = memo(PayoffCurveChartBase);
PayoffCurveChart.displayName = "PayoffCurveChart";

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

export interface HeatmapProps {
  cells: HeatCell[];
  columns?: number;
  width?: number;
  cellHeight?: number;
}

function HeatmapBase({ cells, columns = 6, width = 640, cellHeight = 46 }: HeatmapProps) {
  const plot = useMemo(() => {
    if (cells.length === 0) return null;
    const rows = Math.ceil(cells.length / columns);
    const cellWidth = width / columns;
    const height = rows * cellHeight;
    const scale = Math.max(1, ...cells.map((cell) => Math.abs(cell.value)));
    return {
      cellWidth,
      height,
      tiles: cells.map((cell, index) => ({
        key: `${cell.label}-${index}`,
        x: (index % columns) * cellWidth,
        y: Math.floor(index / columns) * cellHeight,
        fill: heatColor(cell.value, scale),
        label: cell.label,
        valueText: `${cell.value >= 0 ? "+" : ""}${cell.value.toFixed(2)}%`,
        valueTone: cell.value >= 0 ? CHART_COLORS.bull : CHART_COLORS.bear,
        sub: cell.sub,
      })),
    };
  }, [cells, columns, width, cellHeight]);

  if (plot === null) return <EmptyPlot width={width} height={120} label="NO HEATMAP" />;

  return (
    <svg viewBox={`0 0 ${width} ${plot.height}`} className="w-full" role="img" aria-label="heatmap">
      {plot.tiles.map((tile) => (
        <g key={tile.key}>
          <rect
            x={tile.x + 1}
            y={tile.y + 1}
            width={plot.cellWidth - 2}
            height={cellHeight - 2}
            fill={tile.fill}
            stroke={CHART_COLORS.hair}
            strokeWidth="1"
          />
          <text x={tile.x + 6} y={tile.y + 17} fill={CHART_COLORS.ink} fontSize="9.5" fontWeight="600">
            {tile.label}
          </text>
          <text x={tile.x + 6} y={tile.y + 31} fill={tile.valueTone} fontSize="9">
            {tile.valueText}
          </text>
          {tile.sub ? (
            <text
              x={tile.x + plot.cellWidth - 6}
              y={tile.y + cellHeight - 8}
              textAnchor="end"
              fill={CHART_COLORS.ink3}
              fontSize="8"
            >
              {tile.sub}
            </text>
          ) : null}
        </g>
      ))}
    </svg>
  );
}

export const Heatmap = memo(HeatmapBase);
Heatmap.displayName = "Heatmap";

export interface EquityDrawdownCurveProps {
  equity: number[];
  drawdown: number[];
  height?: number;
  width?: number;
}

function EquityDrawdownCurveBase({ equity, drawdown, height = 220, width = 640 }: EquityDrawdownCurveProps) {
  const plot = useMemo(() => {
    if (equity.length === 0) return null;
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
    return {
      pad,
      topHeight,
      maxDrawdown,
      equityPath,
      barWidth,
      areaPath: `${equityPath} L${width - pad},${topHeight - pad} L${pad},${topHeight - pad} Z`,
      underwater: drawdown.map((value, index) => ({
        key: index,
        x: pad + index * step,
        y: bottomTop,
        height: Math.max(1, (Math.abs(value) / maxDrawdown) * (bottomHeight - 2)),
      })),
    };
  }, [equity, drawdown, height, width]);

  if (plot === null) return <EmptyPlot width={width} height={height} label="NO EQUITY CURVE" />;

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label="equity and drawdown curve">
      <line x1={0} x2={width} y1={plot.topHeight} y2={plot.topHeight} stroke={CHART_COLORS.hair} strokeWidth="1" />
      <path d={plot.areaPath} fill={CHART_COLORS.accent} opacity="0.07" />
      <path d={plot.equityPath} fill="none" stroke={CHART_COLORS.accent} strokeWidth="1.5" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
      {plot.underwater.map((bar) => (
        <rect key={bar.key} x={bar.x} y={bar.y} width={plot.barWidth} height={bar.height} fill={CHART_COLORS.bear} opacity="0.5" />
      ))}
      <text x={plot.pad} y={height - 1} fill={CHART_COLORS.ink3} fontSize="8">
        UNDERWATER
      </text>
      <text x={width - plot.pad} y={height - 1} textAnchor="end" fill={CHART_COLORS.ink3} fontSize="8">
        MAX DD {plot.maxDrawdown.toFixed(2)}%
      </text>
    </svg>
  );
}

export const EquityDrawdownCurve = memo(EquityDrawdownCurveBase);
EquityDrawdownCurve.displayName = "EquityDrawdownCurve";

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
