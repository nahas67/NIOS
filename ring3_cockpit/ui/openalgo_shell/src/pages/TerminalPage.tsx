/**
 * Institutional market terminal.
 *
 * A dual-vendor candle tape with MA5/10/20 and VWAP, the ten-level L2 ladder
 * and a Lee-Ready time & sales print. Every frame is requested through the
 * gateway; the cockpit never reads an exchange socket directly.
 */

import { useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { isRecord, num, rows, str } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { toast } from "@/lib/toast";
import {
  Btn,
  DataGrid,
  EmptyCleanSlate,
  Field,
  MetricTile,
  SectionHeader,
  StatusBadge,
  TerminalPanel,
} from "@/design-system/primitives";
import { CandlestickChart, type Candle } from "@/charts/CandlestickChart";
import { DepthLadderChart, type DepthLevel } from "@/charts/DepthLadderChart";

function candleFrom(row: Record<string, unknown>): Candle {
  return {
    t: str(row, "ts", str(row, "t", "-")),
    o: num(row, "open", num(row, "o", 0)),
    h: num(row, "high", num(row, "h", 0)),
    l: num(row, "low", num(row, "l", 0)),
    c: num(row, "close", num(row, "c", 0)),
    v: num(row, "volume", num(row, "v", 0)),
  };
}

function levelFrom(value: unknown): DepthLevel {
  if (Array.isArray(value)) {
    return { price: Number(value[0]) || 0, qty: Number(value[1]) || 0 };
  }
  if (isRecord(value)) {
    return { price: num(value, "price", 0), qty: num(value, "qty", num(value, "size", 0)) };
  }
  return { price: 0, qty: 0 };
}

export function TerminalPage(): ReactNode {
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [frame, setFrame] = useState<Record<string, unknown> | null>(null);
  const page = useApi(() => api.terminal(), []);

  const payload = frame ?? page.data;
  const activeSymbol = frame ? symbol : str(page.data, "default_symbol", symbol);
  const watchlist = rows(page.data, "watchlist");
  const candles = rows(payload, "candles").map(candleFrom);
  const bids = rows(payload, "depth.bids").map(levelFrom);
  const asks = rows(payload, "depth.asks").map(levelFrom);
  const tape = rows(payload, "tape");
  const last = candles.length > 0 ? candles[candles.length - 1] : null;
  const first = candles.length > 0 ? candles[0] : null;
  const changePct = last && first && first.c > 0 ? ((last.c - first.c) / first.c) * 100 : 0;
  const spreadBps =
    bids[0] && asks[0] && bids[0].price > 0
      ? ((asks[0].price - bids[0].price) / bids[0].price) * 10_000
      : 0;

  const quote = async () => {
    try {
      const next = await api.terminalQuote({ symbol, bars: 120, depth: 10 });
      setFrame(next);
      toast(`Grounded frame captured for ${symbol}`, "success");
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "quote rejected", "error");
    }
  };

  return (
    <div className="nx-page">
      <SectionHeader
        kicker="RING 1 - MARKET DATA"
        title="Market terminal, L2 depth and charting"
        sub="Two vendors normalised independently. A resting quote is a hypothesis; only executed flow is treated as liquidity."
        actions={
          <>
            <StatusBadge tone="accent">DUAL VENDOR</StatusBadge>
            <Btn size="sm" tone="ghost" onClick={quote}>
              Capture frame
            </Btn>
          </>
        }
      />

      <div className="nx-grid nx-grid--4">
        <MetricTile
          label={`${activeSymbol} last`}
          value={last ? last.c.toFixed(2) : "-"}
          delta={changePct}
          sub="session change"
          tone={changePct >= 0 ? "bull" : "bear"}
        />
        <MetricTile label="Bars captured" value={String(candles.length)} sub="1-minute dual-vendor frames" />
        <MetricTile
          label="Book spread"
          value={`${spreadBps.toFixed(1)} bps`}
          sub={`${bids.length} bid / ${asks.length} ask levels`}
          badge={<StatusBadge tone={spreadBps < 5 ? "bull" : "accent"}>L2</StatusBadge>}
        />
        <MetricTile
          label="Cancel-to-fill"
          value={num(payload, "cancel_to_fill", 0).toFixed(2)}
          sub="gate trips at 3.0"
          badge={<StatusBadge tone="muted">OFI 0.65</StatusBadge>}
        />
      </div>

      <div className="nx-grid nx-grid--3-2">
        <TerminalPanel
          title={`${activeSymbol} candle tape`}
          subtitle="MA5 / MA10 / MA20 / VWAP - flat 1px overlays"
          actions={
            <span className="nx-chart-legend">
              <StatusBadge tone="bull">BULL</StatusBadge>
              <StatusBadge tone="bear">BEAR</StatusBadge>
            </span>
          }
        >
          <CandlestickChart bars={candles} symbol={activeSymbol} />
        </TerminalPanel>

        <div className="nx-stack">
          <TerminalPanel title="Quote request" subtitle="POST /api/v2/control/terminal/quote">
            <Field label="Symbol">
              <input
                className="nx-input"
                value={symbol}
                onChange={(event) => setSymbol(event.target.value.toUpperCase())}
                placeholder="BTCUSDT"
              />
            </Field>
            <Btn tone="primary" block onClick={quote} style={{ marginTop: 8 }}>
              Request grounded frame
            </Btn>
            <p className="nx-xs nx-muted" style={{ marginTop: 6 }}>
              {str(page.data, "law", "sec_018 Law 1 - never trust a single feed or a resting order")}
            </p>
          </TerminalPanel>

          <TerminalPanel title="Watchlist" subtitle="Four silos, one tape" flush>
            <DataGrid
              columns={[
                {
                  key: "symbol",
                  label: "Symbol",
                  render: (row: Record<string, unknown>) => (
                    <span className="nx-strong">{str(row, "symbol", "-")}</span>
                  ),
                },
                {
                  key: "silo",
                  label: "Silo",
                  render: (row: Record<string, unknown>) => (
                    <span className="nx-dim">{str(row, "silo", "-")}</span>
                  ),
                },
                {
                  key: "venue",
                  label: "Venue",
                  align: "right",
                  render: (row: Record<string, unknown>) => (
                    <span className="nx-muted nx-xs">{str(row, "venue", str(row, "primary_venue", "-"))}</span>
                  ),
                },
              ]}
              rows={watchlist}
              getKey={(row) => str(row, "symbol", String(watchlist.indexOf(row)))}
              empty={<EmptyCleanSlate title="Watchlist empty" body="No instrument is armed for capture." />}
            />
          </TerminalPanel>
        </div>
      </div>

      <div className="nx-grid nx-grid--2">
        <TerminalPanel title="10-level L2 ladder" subtitle="Bid forest tint versus ask burgundy tint">
          <DepthLadderChart bids={bids} asks={asks} />
        </TerminalPanel>

        <TerminalPanel title="Time & sales" subtitle="Lee-Ready aggressor classification" flush>
          <DataGrid
            columns={[
              {
                key: "ts",
                label: "Time",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-mono nx-muted">{str(row, "ts", str(row, "time", "-")).slice(11, 19)}</span>
                ),
              },
              {
                key: "price",
                label: "Price",
                align: "right",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-num">{num(row, "price", 0).toFixed(2)}</span>
                ),
              },
              {
                key: "qty",
                label: "Size",
                align: "right",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-num">{num(row, "qty", num(row, "size", 0)).toLocaleString("en-US")}</span>
                ),
              },
              {
                key: "side",
                label: "Aggressor",
                align: "right",
                render: (row: Record<string, unknown>) => {
                  const side = str(row, "side", "UNKNOWN").toUpperCase();
                  const bull = side === "BUY" || side === "BID";
                  return <StatusBadge tone={bull ? "bull" : "bear"}>{side}</StatusBadge>;
                },
              },
            ]}
            rows={tape}
            getKey={(row, index) => `${str(row, "ts", "")}-${index}`}
            empty={
              <EmptyCleanSlate
                title="No prints captured"
                body="Time and sales populates from executed taker flow. A resting order never appears here."
                hint="TakerBuyRatio_5s >= 0.65"
              />
            }
          />
        </TerminalPanel>
      </div>
    </div>
  );
}
