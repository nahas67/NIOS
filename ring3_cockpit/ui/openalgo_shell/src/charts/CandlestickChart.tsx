/**
 * Candlestick tape.
 *
 * Bullish bodies are emerald-sage, bearish bodies coral-crimson, moving averages
 * and VWAP are flat 1px lines, volume is a flat bar row. Hover draws a hairline
 * crosshair and a matte readout - no gradient, no glow.
 */

import { useMemo, useState, type ReactNode } from "react";
import { CHART, ChartLegend, extent, linePath, plotGrid, scale } from "./kit";

export interface Candle {
  t: string | number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

export interface CandlestickChartProps {
  bars: Candle[];
  height?: number;
  symbol?: string;
}

const BOX = { x: 54, y: 12, w: 640, h: 186 };
const VOL = { x: 54, y: 218, w: 640, h: 44 };

function sma(values: number[], window: number): Array<number | null> {
  const output: Array<number | null> = [];
  let sum = 0;
  for (let index = 0; index < values.length; index += 1) {
    sum += values[index];
    if (index >= window) sum -= values[index - window];
    output.push(index >= window - 1 ? sum / window : null);
  }
  return output;
}

export function CandlestickChart({ bars, height = 300, symbol = "NIOS" }: CandlestickChartProps): ReactNode {
  const [hover, setHover] = useState<number | null>(null);

  const model = useMemo(() => {
    if (bars.length === 0) return null;
    const highs = bars.map((bar) => bar.h);
    const lows = bars.map((bar) => bar.l);
    const [lo, hi] = extent([...highs, ...lows]);
    const pad = (hi - lo) * 0.06;
    const y = scale(lo - pad, hi + pad, BOX.h);
    const step = BOX.w / bars.length;
    const bodyW = Math.max(1.6, Math.min(9, step * 0.62));
    const closes = bars.map((bar) => bar.c);
    const ma5 = sma(closes, 5);
    const ma10 = sma(closes, 10);
    const ma20 = sma(closes, 20);
    let pv = 0;
    let vol = 0;
    const vwap: Array<number | null> = [];
    for (let index = 0; index < bars.length; index += 1) {
      const bar = bars[index];
      pv += bar.c * bar.v;
      vol += bar.v;
      vwap.push(vol > 0 ? pv / vol : null);
    }
    const maxVol = Math.max(...bars.map((bar) => bar.v), 1);
    const x = (index: number) => BOX.x + step * (index + 0.5);
    const yPrice = (value: number) => BOX.y + BOX.h - y(value);
    const trace = (series: Array<number | null>) =>
      linePath(
        series
          .map((value, index) => (value === null ? null : [x(index), yPrice(value)] as [number, number]))
          .filter((point): point is [number, number] => point !== null),
      );
    return {
      step,
      bodyW,
      x,
      yPrice,
      lo: lo - pad,
      hi: hi + pad,
      maxVol,
      ma5: trace(ma5),
      ma10: trace(ma10),
      ma20: trace(ma20),
      vwap: trace(vwap),
    };
  }, [bars]);

  if (!model) {
    return (
      <div className="nx-stack">
        <svg className="nx-chart" viewBox="0 0 720 300" style={{ height }} role="img" aria-label="Candlestick tape empty">
          <title>Candlestick tape - no frames</title>
          {plotGrid(BOX)}
          {plotGrid(VOL, 1, 6)}
          <text x={BOX.x + BOX.w / 2} y={BOX.y + BOX.h / 2} textAnchor="middle" fill={CHART.axis} fontSize={11}>
            NO TAPE CAPTURED - awaiting the first dual-vendor frame
          </text>
        </svg>
        <ChartLegend
          items={[
            { label: "MA5", color: CHART.accent },
            { label: "MA10", color: CHART.text },
            { label: "MA20", color: CHART.axis },
            { label: "VWAP", color: CHART.bull },
          ]}
        />
      </div>
    );
  }

  const hovered = hover !== null ? bars[hover] : null;

  return (
    <div className="nx-stack">
      <svg
        className="nx-chart"
        viewBox="0 0 720 300"
        style={{ height }}
        role="img"
        aria-label={`${symbol} candlestick tape`}
        onMouseMove={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          const local = ((event.clientX - rect.left) / rect.width) * 720;
          const index = Math.round((local - BOX.x) / model.step - 0.5);
          setHover(index >= 0 && index < bars.length ? index : null);
        }}
        onMouseLeave={() => setHover(null)}
      >
        <title>{`${symbol} candlestick tape`}</title>
        {plotGrid(BOX)}
        {plotGrid(VOL, 1, 6)}

        {[0, 0.25, 0.5, 0.75, 1].map((fraction) => {
          const price = model.hi - (model.hi - model.lo) * fraction;
          return (
            <text
              key={fraction}
              x={BOX.x - 7}
              y={BOX.y + BOX.h * fraction + 3}
              textAnchor="end"
              fill={CHART.axis}
              fontSize={8.5}
            >
              {price.toFixed(2)}
            </text>
          );
        })}

        {bars.map((bar, index) => {
          const up = bar.c >= bar.o;
          const color = up ? CHART.bull : CHART.bear;
          const cx = model.x(index);
          const top = model.yPrice(Math.max(bar.o, bar.c));
          const bottom = model.yPrice(Math.min(bar.o, bar.c));
          const bodyH = Math.max(1, bottom - top);
          const volH = (bar.v / model.maxVol) * VOL.h;
          return (
            <g key={`${bar.t}-${index}`}>
              <line
                x1={cx}
                y1={model.yPrice(bar.h)}
                x2={cx}
                y2={model.yPrice(bar.l)}
                stroke={color}
                strokeWidth={1}
              />
              <rect
                x={cx - model.bodyW / 2}
                y={top}
                width={model.bodyW}
                height={bodyH}
                fill={up ? CHART.bullFill : CHART.bearFill}
                stroke={color}
                strokeWidth={1}
              />
              <rect
                x={cx - model.bodyW / 2}
                y={VOL.y + VOL.h - volH}
                width={model.bodyW}
                height={volH}
                fill={up ? CHART.bullFill : CHART.bearFill}
                stroke={color}
                strokeWidth={0.5}
              />
            </g>
          );
        })}

        <path d={model.ma5} fill="none" stroke={CHART.accent} strokeWidth={1} />
        <path d={model.ma10} fill="none" stroke={CHART.text} strokeWidth={1} />
        <path d={model.ma20} fill="none" stroke={CHART.axis} strokeWidth={1} />
        <path d={model.vwap} fill="none" stroke={CHART.bull} strokeWidth={1} strokeDasharray="3 3" />

        {hover !== null && hovered ? (
          <g>
            <line
              x1={model.x(hover)}
              y1={BOX.y}
              x2={model.x(hover)}
              y2={VOL.y + VOL.h}
              stroke={CHART.text}
              strokeWidth={1}
              strokeDasharray="2 3"
            />
            <line
              x1={BOX.x}
              y1={model.yPrice(hovered.c)}
              x2={BOX.x + BOX.w}
              y2={model.yPrice(hovered.c)}
              stroke={CHART.text}
              strokeWidth={1}
              strokeDasharray="2 3"
            />
            <rect
              x={Math.min(model.x(hover) + 8, BOX.x + BOX.w - 150)}
              y={BOX.y + 6}
              width={142}
              height={58}
              rx={4}
              fill={CHART.surface}
              stroke={CHART.grid}
              strokeWidth={1}
            />
            <text
              x={Math.min(model.x(hover) + 16, BOX.x + BOX.w - 142)}
              y={BOX.y + 20}
              fill={CHART.strong}
              fontSize={9}
            >
              O {hovered.o.toFixed(2)}  H {hovered.h.toFixed(2)}
            </text>
            <text
              x={Math.min(model.x(hover) + 16, BOX.x + BOX.w - 142)}
              y={BOX.y + 32}
              fill={CHART.strong}
              fontSize={9}
            >
              L {hovered.l.toFixed(2)}  C {hovered.c.toFixed(2)}
            </text>
            <text
              x={Math.min(model.x(hover) + 16, BOX.x + BOX.w - 142)}
              y={BOX.y + 44}
              fill={CHART.text}
              fontSize={9}
            >
              VOL {hovered.v.toLocaleString("en-US")}
            </text>
            <text
              x={Math.min(model.x(hover) + 16, BOX.x + BOX.w - 142)}
              y={BOX.y + 56}
              fill={CHART.axis}
              fontSize={9}
            >
              {String(hovered.t)}
            </text>
          </g>
        ) : null}

        <text x={BOX.x} y={296} fill={CHART.axis} fontSize={8.5}>
          {String(bars[0]?.t ?? "")}
        </text>
        <text x={BOX.x + BOX.w} y={296} fill={CHART.axis} fontSize={8.5} textAnchor="end">
          {String(bars[bars.length - 1]?.t ?? "")}
        </text>
      </svg>
      <ChartLegend
        items={[
          { label: "MA5", color: CHART.accent },
          { label: "MA10", color: CHART.text },
          { label: "MA20", color: CHART.axis },
          { label: "VWAP", color: CHART.bull, dashed: true },
          { label: "Bull", color: CHART.bull },
          { label: "Bear", color: CHART.bear },
        ]}
      />
    </div>
  );
}
