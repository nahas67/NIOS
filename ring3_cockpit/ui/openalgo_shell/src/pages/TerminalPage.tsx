/**
 * Institutional Market Terminal (sec_018 Law 1).
 *
 * One grounded frame per symbol: candles with MA5/10/20 and VWAP, a Lee-Ready
 * time & sales tape, the ten-level L2 ladder, and — on the paper surface only —
 * a quick order ticket that posts through the ring 0 paper endpoint. The live
 * vault gets a routing note instead of a synthetic ticket.
 */

import { useState, type ReactNode } from "react";
import { ArrowRight } from "lucide-react";
import { api, postPaperOrder } from "@/lib/api";
import { useAction, useResource } from "@/lib/use-api";
import { navigate, pagePath, useLocation } from "@/lib/router";
import { int, num, price, text } from "@/lib/format";
import type { PaperOrderReceipt, TerminalFrame } from "@/lib/types";
import {
  EmptyCleanSlate,
  ErrorBlock,
  Freshness,
  GlassPanel,
  LoadingBlock,
  SectionHeader,
  StatusPill,
  TabularValue,
} from "@/design-system/primitives";
import { CandlestickChart } from "@/charts/CandlestickChart";
import { DepthLadderChart } from "@/charts/DepthLadderChart";
import { pushToast, toastError } from "@/lib/toast";

function isBuyPrint(side: unknown, classification: unknown): boolean {
  return `${text(side, "")} ${text(classification, "")}`.toUpperCase().includes("BUY");
}

export function TerminalPage(): ReactNode {
  const location = useLocation();
  const isLive = location.workspace === "live";

  const initial = useResource("terminal-page", () => api.terminal(), 8000);
  const quote = useAction<TerminalFrame>();
  const order = useAction<PaperOrderReceipt>();

  const [frame, setFrame] = useState<TerminalFrame | null>(null);
  const [symbol, setSymbol] = useState("");
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [qty, setQty] = useState("1");
  const [style, setStyle] = useState<"MAKER_PEG" | "TWAP_ICEBERG">("MAKER_PEG");

  const watchlist = initial.data?.watchlist ?? [];
  const polledFrame = initial.data?.frame ?? null;
  const activeFrame = frame && frame.symbol !== polledFrame?.symbol ? frame : (polledFrame ?? frame);
  const activeSymbol = activeFrame ? activeFrame.symbol : "";
  const selectedSymbol = symbol || activeSymbol || initial.data?.default_symbol || "";

  const groups = new Map<string, typeof watchlist>();
  for (const row of watchlist) {
    const bucket: typeof watchlist = groups.get(row.silo) ?? [];
    bucket.push(row);
    groups.set(row.silo, bucket);
  }

  const loadSymbol = async (next: string): Promise<void> => {
    setSymbol(next);
    const nextFrame = await quote.run(() => api.terminalQuote({ symbol: next }));
    if (nextFrame) {
      setFrame(nextFrame);
      return;
    }
    if (quote.error) toastError("Quote rejected", quote.error);
  };

  const submitOrder = async (): Promise<void> => {
    if (!activeFrame) return;
    const quantity = Number(qty);
    if (!Number.isFinite(quantity) || quantity <= 0) {
      pushToast("Enter a positive quantity", "The ticket submits a single paper order", "warn");
      return;
    }
    const receipt = await order.run(() =>
      postPaperOrder({
        silo: activeFrame.silo,
        symbol: activeFrame.symbol,
        side,
        quantity,
        order_style: style,
      }),
    );
    if (receipt) {
      pushToast("Paper order staged", `${text(receipt.status, "ACK")} · ${text(receipt.order_id, "no id")}`, "ok");
      initial.reload();
      return;
    }
    if (order.error) toastError("Order rejected", order.error);
  };

  const sessionChange = activeFrame ? activeFrame.quote.session_change_bps : 0;

  return (
    <>
      <SectionHeader
        eyebrow={`ring1 · ${location.workspace} workspace · lee-ready tape`}
        title="Institutional Market Terminal"
        subtitle="Candles with MA5/10/20 and VWAP, a Lee-Ready time & sales tape and the ten-level L2 depth ladder off one dual-vendor frame."
        actions={
          <>
            <StatusPill value={isLive ? "LIVE SURFACE" : "PAPER TAPE"} tone={isLive ? "bad" : "accent"} />
            <button
              type="button"
              className="btn btn-sm"
              onClick={initial.reload}
              disabled={initial.loading && initial.data === null}
              aria-label="Reload the terminal watchlist and frame"
            >
              {initial.loading && initial.data === null ? "Loading…" : "Reload"}
            </button>
          </>
        }
      />

      {initial.error ? <ErrorBlock message={initial.error} onRetry={initial.reload} /> : null}

      {!activeFrame ? (
        initial.loading ? (
          <LoadingBlock rows={4} />
        ) : (
          <EmptyCleanSlate
            title="No terminal frame yet"
            body="The terminal returns one grounded frame per symbol. Request the default watchlist frame to open the tape."
            action={
              <button
                type="button"
                className="btn btn-sm btn-accent"
                onClick={initial.reload}
                aria-label="Request the default terminal frame"
              >
                Request frame
              </button>
            }
          />
        )
      ) : (
        <div className="split-3">
          <GlassPanel title="Watchlist" eyebrow={`${int(watchlist.length)} instruments · ${int(groups.size)} silos`}>
            {watchlist.length === 0 ? (
              <EmptyCleanSlate title="Watchlist empty" body="No instrument is registered for this workspace yet." />
            ) : (
              <div className="flex max-h-[620px] flex-col gap-2 overflow-y-auto pr-1">
                {[...groups.entries()].map(([silo, rows]) => (
                  <div key={silo} className="flex flex-col gap-1">
                    <p className="eyebrow">{silo}</p>
                    {rows.map((row) => {
                      const selected = row.symbol === selectedSymbol;
                      return (
                        <button
                          key={row.symbol}
                          type="button"
                          aria-pressed={selected}
                          aria-label={`Load ${row.symbol} ${row.asset_class} frame from ${row.silo}`}
                          onClick={() => void loadSymbol(row.symbol)}
                          className={`flex w-full items-center justify-between gap-2 rounded-[var(--r-xs)] px-1.5 py-1 text-left ${
                            selected
                              ? "bg-[var(--accent-soft)] text-[var(--ink)] shadow-[inset_0_0_0_1px_var(--line-strong)]"
                              : "text-[var(--ink-soft)] hover:bg-[rgba(255,255,255,0.04)]"
                          }`}
                        >
                          <span className="min-w-0">
                            <span className="mono block truncate text-[11px]">{row.symbol}</span>
                            <span className="mono block text-[8.5px] tracking-[0.1em] text-[var(--dim)] uppercase">
                              {row.asset_class}
                            </span>
                          </span>
                          <span className="mono tnum text-[10.5px]">{price(row.reference)}</span>
                        </button>
                      );
                    })}
                  </div>
                ))}
              </div>
            )}
          </GlassPanel>

          <GlassPanel
            title={`${activeFrame.symbol} · ${activeFrame.silo}`}
            eyebrow={`${activeFrame.asset_class} · ring1 dual-vendor`}
            actions={
              <Freshness
                generatedAtNs={activeFrame.generated_at_ns}
                onRefresh={() => void loadSymbol(activeSymbol)}
                busy={quote.busy}
              />
            }
          >
            <div className="flex flex-col gap-3">
              <div className="row-between flex-wrap gap-2">
                <div className="flex flex-wrap items-baseline gap-2.5">
                  <span className="mono tnum text-[26px] leading-none font-semibold text-[var(--ink)]">
                    {price(activeFrame.quote.last)}
                  </span>
                  <TabularValue
                    value={activeFrame.quote.session_change_bps}
                    kind="bps"
                    digits={1}
                    signTone
                    className="text-[12px]"
                  />
                  <StatusPill
                    value={sessionChange >= 0 ? "SESSION UP" : "SESSION DOWN"}
                    tone={sessionChange >= 0 ? "ok" : "bad"}
                  />
                </div>
                <div className="flex flex-wrap items-center gap-3">
                  <span className="mono text-[10px] text-[var(--dim)]">
                    mid <TabularValue value={activeFrame.quote.mid} kind="price" />
                  </span>
                  <span className="mono text-[10px] text-[var(--dim)]">
                    spread <TabularValue value={activeFrame.quote.spread_bps} kind="bps" digits={1} />
                  </span>
                  <span className="mono text-[10px] text-[var(--dim)]">
                    ref <TabularValue value={activeFrame.quote.reference} kind="price" />
                  </span>
                </div>
              </div>

              <CandlestickChart
                candles={activeFrame.candles}
                series={activeFrame.series}
                symbol={activeFrame.symbol}
                height={330}
              />

              <div>
                <div className="row-between mb-1 flex-wrap gap-2">
                  <p className="eyebrow">Lee-Ready tape · last 24 prints</p>
                  <p className="mono text-[10px] whitespace-nowrap">
                    <span className="text-[var(--pos)]">{int(activeFrame.tape_summary.buy_initiated)} buy</span>
                    <span className="text-[var(--dim)]"> · </span>
                    <span className="text-[var(--neg)]">{int(activeFrame.tape_summary.sell_initiated)} sell</span>
                    <span className="text-[var(--dim)]"> · ratio </span>
                    <span className="tnum text-[var(--ink-soft)]">
                      {num(activeFrame.tape_summary.buy_ratio * 100, 1)}%
                    </span>
                  </p>
                </div>
                <div
                  className="tape-row text-[8.5px] tracking-[0.1em] text-[var(--dim)] uppercase"
                  aria-hidden="true"
                >
                  <span>seq</span>
                  <span>price</span>
                  <span className="text-right">qty</span>
                  <span className="text-right">side</span>
                </div>
                {activeFrame.tape.length === 0 ? (
                  <p className="mono px-1 py-3 text-[10.5px] text-[var(--dim)]">No prints in this frame.</p>
                ) : (
                  <ol className="max-h-[240px] overflow-y-auto" aria-label="Last 24 tape prints">
                    {activeFrame.tape
                      .slice(-24)
                      .reverse()
                      .map((print) => {
                        const buy = isBuyPrint(print.side, print.classification);
                        return (
                          <li
                            key={print.sequence}
                            className={`tape-row ${buy ? "tape-buy" : "tape-sell"}`}
                            aria-label={`Print ${print.sequence}: ${price(print.price)} for ${num(print.quantity, 2)} ${
                              buy ? "buy" : "sell"
                            } initiated`}
                          >
                            <span className="text-[var(--dim)]">{int(print.sequence)}</span>
                            <span>{price(print.price)}</span>
                            <span className="text-right">{num(print.quantity, 2)}</span>
                            <span className="text-right">{buy ? "BUY" : "SELL"}</span>
                          </li>
                        );
                      })}
                  </ol>
                )}
              </div>
            </div>
          </GlassPanel>

          <div className="flex flex-col gap-3">
            <GlassPanel title="L2 depth ladder" eyebrow={`${int(activeFrame.depth.level_count)} levels · ring1`}>
              <DepthLadderChart
                bids={activeFrame.depth.bids}
                asks={activeFrame.depth.asks}
                bidQuantityTotal={activeFrame.depth.bid_quantity_total}
                askQuantityTotal={activeFrame.depth.ask_quantity_total}
                imbalanceRatio={activeFrame.depth.imbalance_ratio}
                imbalanceRegime={activeFrame.depth.imbalance_regime}
                levels={10}
              />
            </GlassPanel>

            {isLive ? (
              <GlassPanel title="Live order routing" eyebrow="ring0 signature required">
                <div className="flex flex-col gap-2">
                  <p className="rounded-[var(--r-md)] border border-[var(--line-strong)] bg-[var(--panel-sunken)] p-3 text-[12px] text-[var(--ink-soft)]">
                    Live orders are signed in Orders &amp; Positions — no synthetic ticket on the live surface.
                  </p>
                  <button
                    type="button"
                    className="btn btn-sm self-start"
                    onClick={() => navigate(pagePath("orders", "live"))}
                    aria-label="Open Orders and Positions in the live vault"
                  >
                    Open Orders &amp; Positions
                    <ArrowRight size={11} aria-hidden="true" />
                  </button>
                </div>
              </GlassPanel>
            ) : (
              <GlassPanel title="Quick order ticket" eyebrow="paper · maker-peg default">
                <div className="flex flex-col gap-2">
                  <div className="seg">
                    {(["BUY", "SELL"] as const).map((option) => (
                      <button
                        key={option}
                        type="button"
                        className="seg-item"
                        aria-pressed={side === option}
                        aria-label={`Set order side ${option}`}
                        onClick={() => setSide(option)}
                      >
                        {option}
                      </button>
                    ))}
                  </div>

                  <label className="field">
                    <span className="field-label">Quantity</span>
                    <input
                      className="input tnum"
                      inputMode="decimal"
                      value={qty}
                      onChange={(event) => setQty(event.target.value)}
                      aria-label="Paper order quantity"
                    />
                  </label>

                  <label className="field">
                    <span className="field-label">Order style</span>
                    <select
                      className="select"
                      value={style}
                      onChange={(event) =>
                        setStyle(event.target.value === "TWAP_ICEBERG" ? "TWAP_ICEBERG" : "MAKER_PEG")
                      }
                      aria-label="Paper order style"
                    >
                      <option value="MAKER_PEG">MAKER_PEG</option>
                      <option value="TWAP_ICEBERG">TWAP_ICEBERG</option>
                    </select>
                  </label>

                  <button
                    type="button"
                    className="btn btn-accent"
                    onClick={() => void submitOrder()}
                    disabled={order.busy}
                    aria-label="Submit the paper order"
                  >
                    {order.busy ? "Submitting…" : `Submit ${side}`}
                  </button>

                  {order.error ? <ErrorBlock message={order.error} /> : null}

                  {order.receipt ? (
                    <div className="rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--panel-sunken)] px-2.5 py-2">
                      <div className="row-between gap-2">
                        <span className="eyebrow">paper receipt</span>
                        <StatusPill value={text(order.receipt.status, "ACK")} tone="ok" />
                      </div>
                      <div className="mono mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10.5px] text-[var(--ink-soft)]">
                        <span>{order.receipt.order_id ? order.receipt.order_id.slice(0, 10) : "—"}</span>
                        <span className="text-[var(--dim)]">·</span>
                        <span>
                          fill <TabularValue value={order.receipt.filled_quantity} kind="num" />
                        </span>
                        <span>
                          @ <TabularValue value={order.receipt.fill_price} kind="price" />
                        </span>
                        <span className="text-[var(--dim)]">·</span>
                        <span>
                          net <TabularValue value={order.receipt.net_bps} kind="bps" digits={1} signTone />
                        </span>
                      </div>
                    </div>
                  ) : null}
                </div>
              </GlassPanel>
            )}
          </div>
        </div>
      )}
    </>
  );
}
