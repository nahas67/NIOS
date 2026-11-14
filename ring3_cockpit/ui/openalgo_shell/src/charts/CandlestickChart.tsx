/**
 * OHLCV candles with MA5 / MA10 / MA20 / VWAP overlays and a volume pane.
 * Hand-built SVG: one path per moving average, one rect per candle, a single
 * crosshair that snaps to the nearest bar index.
 */

import type { ReactNode } from "react";
import { isFiniteNumber, num, price } from "@/lib/format";
import {
  ChartTooltip,
  GridLines,
  TooltipRow,
  VB_W,
  areaPath,
  domainOf,
  linePath,
  scale,
  ticks,
  useCrosshair,
} from "./kit";

export interface Candle {
  index?: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface CandlestickChartProps {
  candles: Candle[];
  series?: {
    ma5?: number[];
    ma10?: number[];
    ma20?: number[];
    vwap?: number[];
  };
  symbol?: string;
  height?: number;
}

const MARGIN = { left: 58, right: 14, top: 14 };
const VOLUME_H = 46;
const AXIS_H = 18;

function seriesPath(values: number[] | undefined, count: number, plotW: number, plotH: number, domain: { min: number; max: number }, left: number, top: number): string {
  if (!values || values.length === 0) return "";
  const length = Math.min(count, values.length);
  const points: Array<[number, number]> = [];
  for (let index = 0; index < length; index += 1) {
    const value = values[index];
    if (!isFiniteNumber(value)) continue;
    const x = left + (count === 1 ? plotW / 2 : (index / (count - 1)) * plotW);
    points.push([x, top + plotH - scale(value, domain, plotH)]);
  }
  return linePath(points);
}

export function CandlestickChart({ candles, series, symbol = "—", height = 340 }: CandlestickChartProps): ReactNode {
  const vbHeight = height;
  const plotW = VB_W - MARGIN.left - MARGIN.right;
  const plotH = vbHeight - MARGIN.top - VOLUME_H - AXIS_H;
  const count = candles.length;
  const crosshair = useCrosshair(count);

  if (count === 0) {
    return (
      <div className="empty" style={{ minHeight: height }}>
        <p className="text-[12px] text-[var(--dim)]">No bars in the terminal frame yet.</p>
      </div>
    );
  }

  const highs: number[] = candles.map((candle) => candle.high);
  const lows: number[] = candles.map((candle) => candle.low);
  const overlays = [series?.ma5, series?.ma10, series?.ma20, series?.vwap].flatMap((values) =>
    values ? values.filter(isFiniteNumber) : [],
  );
  const domain = domainOf([...highs, ...lows, ...overlays], 0.04);
  const volumeMax = Math.max(...candles.map((candle) => candle.volume), 1);
  const slot = plotW / count;
  const bodyW = Math.max(1.4, Math.min(8, slot * 0.58));
  const volumeTop = MARGIN.top + plotH + 10;

  const xAt = (index: number) => MARGIN.left + (count === 1 ? plotW / 2 : (index / (count - 1)) * plotW);
  const yAt = (value: number) => MARGIN.top + plotH - scale(value, domain, plotH);

  const hovered = crosshair.index !== null ? candles[crosshair.index] : null;
  const hoveredMa = crosshair.index !== null
    ? {
        ma5: series?.ma5?.[crosshair.index],
        ma10: series?.ma10?.[crosshair.index],
        ma20: series?.ma20?.[crosshair.index],
        vwap: series?.vwap?.[crosshair.index],
      }
    : null;

  return (
    <div className="relative w-full">
      <svg
        viewBox={`0 0 ${VB_W} ${vbHeight}`}
        width="100%"
        height={vbHeight}
        role="img"
        aria-label={`${symbol} candlestick chart with ${count} bars, moving averages and volume`}
        {...crosshair.bind}
        style={{ touchAction: "none" }}
      >
        <GridLines domain={domain} plotH={plotH} plotW={plotW} left={MARGIN.left} top={MARGIN.top} format={(value) => price(value)} />

        {ticks(domain, 4).map((value) => (
          <line
            key={`anchor-${value}`}
            x1={MARGIN.left}
            x2={VB_W - MARGIN.right}
            y1={MARGIN.top + plotH - scale(value, domain, plotH)}
            y2={MARGIN.top + plotH - scale(value, domain, plotH)}
            stroke="rgba(148,163,184,0.05)"
          />
        ))}

        {candles.map((candle, index) => {
          const x = xAt(index);
          const up = candle.close >= candle.open;
          const color = up ? "#34d399" : "#fb7185";
          const yOpen = yAt(candle.open);
          const yClose = yAt(candle.close);
          const bodyTop = Math.min(yOpen, yClose);
          const bodyHeight = Math.max(1, Math.abs(yClose - yOpen));
          const volumeH = (candle.volume / volumeMax) * VOLUME_H;
          return (
            <g key={candle.index ?? index}>
              <line x1={x} x2={x} y1={yAt(candle.high)} y2={yAt(candle.low)} stroke={color} strokeWidth={1} opacity={0.85} />
              <rect
                x={x - bodyW / 2}
                y={bodyTop}
                width={bodyW}
                height={bodyHeight}
                fill={up ? "rgba(52,211,153,0.22)" : "rgba(251,113,133,0.24)"}
                stroke={color}
                strokeWidth={0.9}
              />
              <rect
                x={x - bodyW / 2}
                y={volumeTop + (VOLUME_H - volumeH)}
                width={bodyW}
                height={Math.max(0.6, volumeH)}
                fill={color}
                opacity={0.32}
              />
            </g>
          );
        })}

        {series?.ma5 ? (
          <path d={seriesPath(series.ma5, count, plotW, plotH, domain, MARGIN.left, MARGIN.top)} fill="none" stroke="#22d3ee" strokeWidth={1.2} opacity={0.9} />
        ) : null}
        {series?.ma10 ? (
          <path d={seriesPath(series.ma10, count, plotW, plotH, domain, MARGIN.left, MARGIN.top)} fill="none" stroke="#a78bfa" strokeWidth={1.2} opacity={0.9} />
        ) : null}
        {series?.ma20 ? (
          <path d={seriesPath(series.ma20, count, plotW, plotH, domain, MARGIN.left, MARGIN.top)} fill="none" stroke="#f59e0b" strokeWidth={1.2} opacity={0.85} />
        ) : null}
        {series?.vwap ? (
          <path
            d={seriesPath(series.vwap, count, plotW, plotH, domain, MARGIN.left, MARGIN.top)}
            fill="none"
            stroke="#94a3b8"
            strokeWidth={1.1}
            strokeDasharray="5 4"
            opacity={0.9}
          />
        ) : null}

        {series?.vwap && series.vwap.length > 0 ? (
          <path
            d={areaPath(
              series.vwap
                .map((value, index) => {
                  if (!isFiniteNumber(value)) return null;
                  return [xAt(index), yAt(value)] as [number, number];
                })
                .filter((point): point is [number, number] => point !== null),
              MARGIN.top + plotH,
            )}
            fill="url(#vwap-fade)"
            opacity={0.18}
          />
        ) : null}

        <defs>
          <linearGradient id="vwap-fade" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#94a3b8" stopOpacity="0.55" />
            <stop offset="100%" stopColor="#94a3b8" stopOpacity="0" />
          </linearGradient>
        </defs>

        <line x1={MARGIN.left} x2={VB_W - MARGIN.right} y1={volumeTop - 4} y2={volumeTop - 4} stroke="rgba(148,163,184,0.12)" />
        <text x={MARGIN.left - 6} y={volumeTop + 9} textAnchor="end" className="iso-ring-label" style={{ fontSize: 8 }}>
          VOL
        </text>

        <text x={MARGIN.left} y={vbHeight - 5} className="iso-ring-label" style={{ fontSize: 8 }}>
          t-{count}
        </text>
        <text x={VB_W - MARGIN.right} y={vbHeight - 5} textAnchor="end" className="iso-ring-label" style={{ fontSize: 8 }}>
          now
        </text>

        {crosshair.index !== null ? (
          <g>
            <line x1={crosshair.x} x2={crosshair.x} y1={MARGIN.top} y2={volumeTop + VOLUME_H} stroke="rgba(226,232,240,0.28)" strokeDasharray="3 3" />
            <circle cx={crosshair.x} cy={MARGIN.top} r={2.4} fill="#e2e8f0" />
          </g>
        ) : null}
      </svg>

      <div className="pointer-events-none absolute top-2 left-3 flex flex-wrap items-center gap-2.5">
        {[
          ["MA5", "#22d3ee"],
          ["MA10", "#a78bfa"],
          ["MA20", "#f59e0b"],
          ["VWAP", "#94a3b8"],
        ].map(([label, color]) => (
          <span key={label} className="mono flex items-center gap-1 text-[9px] tracking-[0.08em] text-[var(--dim)]">
            <span className="inline-block h-[2px] w-3 rounded" style={{ background: color }} />
            {label}
          </span>
        ))}
      </div>

      {hovered && crosshair.index !== null ? (
        <ChartTooltip x={crosshair.x}>
          <p className="mono mb-1 text-[9px] tracking-[0.1em] text-[var(--dim)] uppercase">bar {crosshair.index + 1}</p>
          <TooltipRow label="O" value={price(hovered.open)} />
          <TooltipRow label="H" value={price(hovered.high)} tone="text-[var(--pos)]" />
          <TooltipRow label="L" value={price(hovered.low)} tone="text-[var(--neg)]" />
          <TooltipRow label="C" value={price(hovered.close)} />
          <TooltipRow label="Vol" value={num(hovered.volume, 0)} />
          {hoveredMa?.ma5 !== undefined ? <TooltipRow label="MA5" value={price(hoveredMa.ma5)} tone="text-[#22d3ee]" /> : null}
          {hoveredMa?.vwap !== undefined ? <TooltipRow label="VWAP" value={price(hoveredMa.vwap)} tone="text-[#94a3b8]" /> : null}
        </ChartTooltip>
      ) : null}
    </div>
  );
}
