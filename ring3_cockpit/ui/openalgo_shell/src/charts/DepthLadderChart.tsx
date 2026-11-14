/**
 * Ten-level L2 depth ladder.
 *
 * Bids grow from the centre to the left in the forest tint, asks from the centre
 * to the right in the burgundy tint. Prices and sizes are tabular; the deepest
 * level sets the 100% bar width.
 */

import type { ReactNode } from "react";
import { CHART, ChartLegend, plotGrid } from "./kit";

export interface DepthLevel {
  price: number;
  qty: number;
}

export interface DepthLadderChartProps {
  bids: DepthLevel[];
  asks: DepthLevel[];
  height?: number;
}

const BOX = { x: 12, y: 14, w: 696, h: 232 };
const CENTER = BOX.x + BOX.w / 2;

export function DepthLadderChart({ bids, asks, height = 270 }: DepthLadderChartProps): ReactNode {
  const cleanBids = bids.slice(0, 10);
  const cleanAsks = asks.slice(0, 10);
  const rows = Math.max(cleanBids.length, cleanAsks.length);
  const maxQty = Math.max(...[...cleanBids, ...cleanAsks].map((level) => level.qty), 1);
  const rowH = rows > 0 ? BOX.h / 10 : BOX.h / 10;
  const half = BOX.w / 2 - 34;

  return (
    <div className="nx-stack">
      <svg
        className="nx-chart"
        viewBox="0 0 720 270"
        style={{ height }}
        role="img"
        aria-label="Level two depth ladder"
      >
        <title>Level two depth ladder</title>
        {plotGrid(BOX, 10, 4)}
        <line x1={CENTER} y1={BOX.y} x2={CENTER} y2={BOX.y + BOX.h} stroke={CHART.grid} strokeWidth={1} />
        <text x={CENTER - 8} y={BOX.y + BOX.h + 16} textAnchor="end" fill={CHART.bull} fontSize={9}>
          BID
        </text>
        <text x={CENTER + 8} y={BOX.y + BOX.h + 16} fill={CHART.bear} fontSize={9}>
          ASK
        </text>

        {rows === 0 ? (
          <text x={CENTER} y={BOX.y + BOX.h / 2} textAnchor="middle" fill={CHART.axis} fontSize={11}>
            BOOK EMPTY - no resting liquidity observed
          </text>
        ) : (
          Array.from({ length: 10 }).map((_, index) => {
            const y = BOX.y + index * rowH + 2;
            const bid = cleanBids[index];
            const ask = cleanAsks[index];
            const bidW = bid ? (bid.qty / maxQty) * half : 0;
            const askW = ask ? (ask.qty / maxQty) * half : 0;
            return (
              <g key={`level-${index}`}>
                {bid ? (
                  <>
                    <rect
                      x={CENTER - 32 - bidW}
                      y={y}
                      width={Math.max(1, bidW)}
                      height={rowH - 4}
                      fill={CHART.bullFill}
                      stroke={CHART.bullLine}
                      strokeWidth={1}
                    />
                    <text x={CENTER - 36} y={y + rowH / 2 + 3} textAnchor="end" fill={CHART.bull} fontSize={9}>
                      {bid.price.toFixed(2)}
                    </text>
                    <text x={CENTER - 42 - bidW} y={y + rowH / 2 + 3} fill={CHART.text} fontSize={8.5}>
                      {bid.qty.toLocaleString("en-US")}
                    </text>
                  </>
                ) : null}
                {ask ? (
                  <>
                    <rect
                      x={CENTER + 32}
                      y={y}
                      width={Math.max(1, askW)}
                      height={rowH - 4}
                      fill={CHART.bearFill}
                      stroke={CHART.bearLine}
                      strokeWidth={1}
                    />
                    <text x={CENTER + 36} y={y + rowH / 2 + 3} fill={CHART.bear} fontSize={9}>
                      {ask.price.toFixed(2)}
                    </text>
                    <text x={CENTER + 42 + askW} y={y + rowH / 2 + 3} fill={CHART.text} fontSize={8.5}>
                      {ask.qty.toLocaleString("en-US")}
                    </text>
                  </>
                ) : null}
                <text x={CENTER - 16} y={y + rowH / 2 + 3} textAnchor="end" fill={CHART.axis} fontSize={8}>
                  {index + 1}
                </text>
                <text x={CENTER + 16} y={y + rowH / 2 + 3} fill={CHART.axis} fontSize={8}>
                  {index + 1}
                </text>
              </g>
            );
          })
        )}

        {cleanBids[0] && cleanAsks[0] ? (
          <text
            x={CENTER}
            y={BOX.y - 4}
            textAnchor="middle"
            fill={CHART.strong}
            fontSize={9}
            fontWeight={600}
          >
            SPREAD {((cleanAsks[0].price - cleanBids[0].price) / cleanBids[0].price * 10_000).toFixed(1)} bps
          </text>
        ) : null}
      </svg>
      <ChartLegend
        items={[
          { label: "Bid wall", color: CHART.bullLine },
          { label: "Ask wall", color: CHART.bearLine },
        ]}
      />
    </div>
  );
}
