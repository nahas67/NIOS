/**
 * Ten-level L2 ladder with a two-sided imbalance meter.
 * Bids are rendered best-first from the spread downwards; asks are mirrored
 * above it, so the book reads the way a trader scans it.
 */

import type { ReactNode } from "react";
import { num, price, ratioPct } from "@/lib/format";
import { StatusPill, type PillTone } from "@/design-system/primitives";

export interface DepthLevel {
  level?: number;
  price: number;
  quantity: number;
}

export interface DepthLadderChartProps {
  bids: DepthLevel[];
  asks: DepthLevel[];
  bidQuantityTotal?: number;
  askQuantityTotal?: number;
  imbalanceRatio?: number;
  imbalanceRegime?: string;
  levels?: number;
}

function regimeTone(regime: string | undefined): PillTone {
  const value = (regime ?? "").toUpperCase();
  if (value.includes("BID") || value.includes("BUY")) return "ok";
  if (value.includes("ASK") || value.includes("SELL")) return "bad";
  return "neutral";
}

export function DepthLadderChart({
  bids,
  asks,
  bidQuantityTotal,
  askQuantityTotal,
  imbalanceRatio,
  imbalanceRegime,
  levels = 10,
}: DepthLadderChartProps): ReactNode {
  const bidRows = bids.slice(0, levels);
  const askRows = asks.slice(0, levels);
  const maxQty = Math.max(
    1,
    ...bidRows.map((row) => row.quantity),
    ...askRows.map((row) => row.quantity),
  );
  const bidTotal = bidQuantityTotal ?? bidRows.reduce((sum, row) => sum + row.quantity, 0);
  const askTotal = askQuantityTotal ?? askRows.reduce((sum, row) => sum + row.quantity, 0);
  const book = bidTotal + askTotal;
  const bidShare = book > 0 ? (bidTotal / book) * 100 : 50;
  const ratio = imbalanceRatio ?? (askTotal > 0 ? bidTotal / askTotal : 0);

  const renderRow = (row: DepthLevel, side: "bid" | "ask") => (
    <div className="depth-row" key={`${side}-${row.level ?? row.price}`}>
      <span className={side === "bid" ? "text-[var(--pos)]" : "text-[var(--neg)]"}>{price(row.price)}</span>
      <span className="depth-track" aria-hidden="true">
        <span
          className={`depth-fill ${side === "bid" ? "depth-bid" : "depth-ask"}`}
          style={{ width: `${Math.max(2, (row.quantity / maxQty) * 100)}%` }}
        />
      </span>
      <span className="text-right text-[var(--ink-soft)]">{num(row.quantity, 2)}</span>
    </div>
  );

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <span className="eyebrow">asks · {askRows.length} levels</span>
        <span className="mono text-[9.5px] text-[var(--dim)]">qty</span>
      </div>
      <div className="flex flex-col-reverse gap-[1px]">{askRows.map((row) => renderRow(row, "ask"))}</div>

      <div className="flex items-center gap-2 py-1">
        <span className="h-[1px] flex-1 bg-[var(--line-strong)]" />
        <span className="mono text-[9px] tracking-[0.12em] text-[var(--dim)] uppercase">spread</span>
        <span className="h-[1px] flex-1 bg-[var(--line-strong)]" />
      </div>

      <div className="flex flex-col gap-[1px]">{bidRows.map((row) => renderRow(row, "bid"))}</div>
      <div className="flex items-center justify-between">
        <span className="eyebrow">bids · {bidRows.length} levels</span>
        <span className="mono text-[9.5px] text-[var(--dim)]">qty</span>
      </div>

      <div className="mt-1 rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--panel-sunken)] p-2.5">
        <div className="row-between">
          <span className="eyebrow">book imbalance</span>
          <StatusPill value={imbalanceRegime ?? (bidShare >= 50 ? "BID_HEAVY" : "ASK_HEAVY")} tone={regimeTone(imbalanceRegime)} />
        </div>
        <div
          className="relative mt-2 h-2 w-full overflow-hidden rounded-full bg-[rgba(251,113,133,0.35)]"
          role="img"
          aria-label={`Bid share ${ratioPct(bidShare / 100)} versus ask share ${ratioPct(1 - bidShare / 100)}, imbalance ratio ${num(ratio, 2)}`}
        >
          <span className="absolute inset-y-0 left-0 rounded-full bg-[var(--pos)]" style={{ width: `${bidShare}%` }} />
          <span className="absolute inset-y-0 left-1/2 w-[1px] bg-[rgba(255,255,255,0.35)]" />
        </div>
        <div className="mono mt-1.5 flex justify-between text-[10px]">
          <span className="text-[var(--pos)]">bid {num(bidTotal, 0)}</span>
          <span className="text-[var(--dim)]">ratio {num(ratio, 2)}×</span>
          <span className="text-[var(--neg)]">ask {num(askTotal, 0)}</span>
        </div>
      </div>
    </div>
  );
}
