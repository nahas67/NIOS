/**
 * 25-point multi-leg expiry P&L curve.
 * Profit and loss are clipped to the zero baseline so the sign of the position
 * is legible without a legend; breakevens, spot and the payoff caps are marked.
 */

import type { ReactNode } from "react";
import { num, price } from "@/lib/format";
import { ChartTooltip, GridLines, TooltipRow, VB_W, domainOf, linePath, scale, useCrosshair } from "./kit";

export interface PayoffPoint {
  expiry_spot: number;
  pnl: number;
  return_bps?: number;
}

export interface PayoffCurveChartProps {
  points: PayoffPoint[];
  breakevens?: number[];
  maxProfit?: number;
  maxLoss?: number;
  spot?: number;
  height?: number;
  currency?: string;
  unboundedUpside?: boolean;
  unboundedDownside?: boolean;
}

const MARGIN = { left: 62, right: 16, top: 16, bottom: 24 };

export function PayoffCurveChart({
  points,
  breakevens = [],
  maxProfit,
  maxLoss,
  spot,
  height = 280,
  currency = "$",
  unboundedUpside = false,
  unboundedDownside = false,
}: PayoffCurveChartProps): ReactNode {
  const plotW = VB_W - MARGIN.left - MARGIN.right;
  const plotH = height - MARGIN.top - MARGIN.bottom;
  const crosshair = useCrosshair(points.length);

  if (points.length === 0) {
    return (
      <div className="empty" style={{ minHeight: height }}>
        <p className="text-[12px] text-[var(--dim)]">Select a strategy to price its expiry payoff.</p>
      </div>
    );
  }

  const spots = points.map((point) => point.expiry_spot);
  const pnls = points.map((point) => point.pnl);
  const yDomain = domainOf([...pnls, 0], 0.12);
  const xMin = Math.min(...spots);
  const xMax = Math.max(...spots);
  const xDomain = { min: xMin, max: xMax === xMin ? xMin + 1 : xMax };
  const xAt = (value: number) => MARGIN.left + scale(value, xDomain, plotW);
  const yAt = (value: number) => MARGIN.top + plotH - scale(value, yDomain, plotH);
  const baseline = yAt(0);

  const pointsXY: Array<[number, number]> = points.map((point) => [xAt(point.expiry_spot), yAt(point.pnl)]);
  const curve = linePath(pointsXY);
  const area = `M${pointsXY[0][0]},${baseline} ${pointsXY.map(([x, y]) => `L${x},${y}`).join(" ")} L${pointsXY[pointsXY.length - 1][0]},${baseline} Z`;

  const hovered = crosshair.index !== null ? points[crosshair.index] : null;

  return (
    <div className="relative w-full">
      <svg
        viewBox={`0 0 ${VB_W} ${height}`}
        width="100%"
        height={height}
        role="img"
        aria-label={`Expiry payoff curve across ${points.length} spot points with ${breakevens.length} breakeven markers`}
        {...crosshair.bind}
        style={{ touchAction: "none" }}
      >
        <defs>
          <clipPath id="payoff-profit">
            <rect x={MARGIN.left} y={MARGIN.top} width={plotW} height={Math.max(0, baseline - MARGIN.top)} />
          </clipPath>
          <clipPath id="payoff-loss">
            <rect x={MARGIN.left} y={baseline} width={plotW} height={Math.max(0, MARGIN.top + plotH - baseline)} />
          </clipPath>
          <linearGradient id="payoff-profit-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#34d399" stopOpacity="0.34" />
            <stop offset="100%" stopColor="#34d399" stopOpacity="0.02" />
          </linearGradient>
          <linearGradient id="payoff-loss-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#fb7185" stopOpacity="0.04" />
            <stop offset="100%" stopColor="#fb7185" stopOpacity="0.34" />
          </linearGradient>
        </defs>

        <GridLines
          domain={yDomain}
          plotH={plotH}
          plotW={plotW}
          left={MARGIN.left}
          top={MARGIN.top}
          count={4}
          format={(value) => `${currency}${num(value, 0)}`}
        />

        <path d={area} fill="url(#payoff-profit-fill)" clipPath="url(#payoff-profit)" />
        <path d={area} fill="url(#payoff-loss-fill)" clipPath="url(#payoff-loss)" />
        <line x1={MARGIN.left} x2={VB_W - MARGIN.right} y1={baseline} y2={baseline} stroke="rgba(226,232,240,0.38)" strokeDasharray="4 4" />

        <path d={curve} fill="none" stroke="var(--accent)" strokeWidth={1.9} />

        {typeof spot === "number" ? (
          <g>
            <line x1={xAt(spot)} x2={xAt(spot)} y1={MARGIN.top} y2={MARGIN.top + plotH} stroke="rgba(148,163,184,0.5)" strokeDasharray="3 3" />
            <text x={xAt(spot)} y={MARGIN.top - 4} textAnchor="middle" className="iso-ring-label" style={{ fontSize: 8 }}>
              spot {price(spot)}
            </text>
          </g>
        ) : null}

        {breakevens.map((level) => (
          <g key={level}>
            <line x1={xAt(level)} x2={xAt(level)} y1={MARGIN.top} y2={MARGIN.top + plotH} stroke="var(--warn)" strokeDasharray="2 3" opacity={0.75} />
            <circle cx={xAt(level)} cy={baseline} r={2.6} fill="var(--warn)" />
            <text x={xAt(level)} y={MARGIN.top + plotH - 4} textAnchor="middle" fill="var(--warn)" style={{ fontSize: 8, fontFamily: "var(--font-mono)" }}>
              BE {price(level)}
            </text>
          </g>
        ))}

        {typeof maxProfit === "number" && maxProfit !== 0 ? (
          <g>
            <line x1={MARGIN.left} x2={VB_W - MARGIN.right} y1={yAt(maxProfit)} y2={yAt(maxProfit)} stroke="#34d399" strokeDasharray="5 5" opacity={0.55} />
            <text x={VB_W - MARGIN.right} y={yAt(maxProfit) - 4} textAnchor="end" fill="#34d399" style={{ fontSize: 8, fontFamily: "var(--font-mono)" }}>
              max profit {currency}
              {num(maxProfit, 0)}
              {unboundedUpside ? " + unbounded" : ""}
            </text>
          </g>
        ) : null}

        {typeof maxLoss === "number" && maxLoss !== 0 ? (
          <g>
            <line x1={MARGIN.left} x2={VB_W - MARGIN.right} y1={yAt(maxLoss)} y2={yAt(maxLoss)} stroke="#fb7185" strokeDasharray="5 5" opacity={0.55} />
            <text
              x={VB_W - MARGIN.right}
              y={yAt(maxLoss) + 10}
              textAnchor="end"
              fill="#fb7185"
              style={{ fontSize: 8, fontFamily: "var(--font-mono)" }}
            >
              max loss {currency}
              {num(maxLoss, 0)}
              {unboundedDownside ? " + unbounded" : ""}
            </text>
          </g>
        ) : null}

        <text x={MARGIN.left} y={height - 6} className="iso-ring-label" style={{ fontSize: 8 }}>
          {price(xMin)}
        </text>
        <text x={VB_W - MARGIN.right} y={height - 6} textAnchor="end" className="iso-ring-label" style={{ fontSize: 8 }}>
          {price(xMax)}
        </text>

        {crosshair.index !== null ? (
          <g>
            <line x1={crosshair.x} x2={crosshair.x} y1={MARGIN.top} y2={MARGIN.top + plotH} stroke="rgba(226,232,240,0.28)" strokeDasharray="3 3" />
            <circle cx={crosshair.x} cy={yAt(points[crosshair.index].pnl)} r={3} fill="var(--accent)" />
          </g>
        ) : null}
      </svg>

      {hovered && crosshair.index !== null ? (
        <ChartTooltip x={crosshair.x}>
          <p className="mono mb-1 text-[9px] tracking-[0.1em] text-[var(--dim)] uppercase">expiry spot</p>
          <TooltipRow label="spot" value={price(hovered.expiry_spot)} />
          <TooltipRow
            label="P&L"
            value={`${hovered.pnl >= 0 ? "+" : ""}${currency}${num(hovered.pnl, 2)}`}
            tone={hovered.pnl >= 0 ? "text-[var(--pos)]" : "text-[var(--neg)]"}
          />
          {typeof hovered.return_bps === "number" ? (
            <TooltipRow label="return" value={`${hovered.return_bps >= 0 ? "+" : ""}${num(hovered.return_bps, 1)} bps`} />
          ) : null}
        </ChartTooltip>
      ) : null}
    </div>
  );
}
