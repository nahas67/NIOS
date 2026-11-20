import { useCallback, useEffect, useMemo, useState } from "react";

import {
  CandlestickChart,
  DepthLadder,
  LineChart,
  type Candle,
  type CandleOverlays,
  type DepthLevel,
  type Series,
} from "../charts";
import { SILOS } from "../config/nav";
import {
  Badge,
  Banner,
  Button,
  Card,
  Cell,
  EmptyState,
  Field,
  KeyValue,
  PageHeader,
  Row,
  SelectInput,
  Spinner,
  Stat,
  StatGrid,
  Table,
  Tabs,
  TextArea,
  TextInput,
} from "../components/ui";
import { api, assertFinite, clampBars, errorMessage } from "../lib/api";
import {
  asArray,
  asRecord,
  asRows,
  clockNs,
  num,
  priceMicros,
  qty,
  safeNumber,
  text,
  usd,
} from "../lib/format";
import { useMarketStream, type StreamFrame } from "../lib/stream";
import type { PageProps } from "./types";

interface Note {
  tone: "neutral" | "bull" | "bear";
  message: string;
}

const TIMEFRAMES = [
  { id: "1m", label: "1m", tv: "1" },
  { id: "5m", label: "5m", tv: "5" },
  { id: "15m", label: "15m", tv: "15" },
  { id: "1D", label: "1D", tv: "1D" },
] as const;

const BAR_LIMIT = clampBars(240);
const MAX_ROWS = 400;
const STUDY_IDS = [
  "STD;RSI",
  "STD;MACD",
  "STD;Supertrend",
  "STD;Bollinger Bands",
  "STD;VWAP Bands",
  "STD;Ichimoku Cloud",
];
const SIDE_OPTIONS = [
  { value: "BUY", label: "BUY" },
  { value: "SELL", label: "SELL" },
];
const TYPE_OPTIONS = [
  { value: "MARKET", label: "MARKET" },
  { value: "LIMIT", label: "LIMIT" },
];

function candleFromTv(row: Record<string, unknown>): Candle | null {
  const open = safeNumber(row.open, Number.NaN);
  const high = safeNumber(row.high, Number.NaN);
  const low = safeNumber(row.low, Number.NaN);
  const close = safeNumber(row.close, Number.NaN);
  if (![open, high, low, close].every(Number.isFinite)) return null;
  return {
    t: safeNumber(row.ts_ns, 0),
    o: open,
    h: high,
    l: low,
    c: close,
    v: safeNumber(row.volume, 0),
  };
}

function candleFromBar(row: Record<string, unknown>): Candle | null {
  const open = safeNumber(row.open_micros, Number.NaN) / 1_000_000;
  const high = safeNumber(row.high_micros, Number.NaN) / 1_000_000;
  const low = safeNumber(row.low_micros, Number.NaN) / 1_000_000;
  const close = safeNumber(row.close_micros, Number.NaN) / 1_000_000;
  if (![open, high, low, close].every(Number.isFinite)) return null;
  return {
    t: safeNumber(row.start_utc_ns, 0),
    o: open,
    h: high,
    l: low,
    c: close,
    v: safeNumber(row.volume_micros, 0) / 1_000_000,
  };
}

function movingAverage(values: number[], window: number): number[] {
  const out: number[] = [];
  let sum = 0;
  for (let index = 0; index < values.length; index += 1) {
    sum += values[index];
    if (index >= window) sum -= values[index - window];
    out.push(sum / Math.min(window, index + 1));
  }
  return out;
}

function vwapSeries(candles: Candle[]): number[] {
  const out: number[] = [];
  let pv = 0;
  let volume = 0;
  for (const candle of candles) {
    const weight = candle.v && candle.v > 0 ? candle.v : 1;
    pv += ((candle.h + candle.l + candle.c) / 3) * weight;
    volume += weight;
    out.push(pv / Math.max(1e-9, volume));
  }
  return out;
}

function levelRows(input: unknown, micros: boolean): DepthLevel[] {
  return asRows(input)
    .map((row) => {
      const price = micros
        ? safeNumber(row.price_micros, Number.NaN) / 1_000_000
        : safeNumber(row.price, Number.NaN);
      const size = micros
        ? safeNumber(row.size_micros, Number.NaN) / 1_000_000
        : safeNumber(row.quantity, Number.NaN);
      if (!Number.isFinite(price) || !Number.isFinite(size)) return null;
      return { price, size };
    })
    .filter((level): level is DepthLevel => level !== null);
}

function studyPoints(input: unknown): number[] {
  const raw = asArray(input);
  const out: number[] = [];
  let last = Number.NaN;
  for (const entry of raw) {
    const value = safeNumber(entry, Number.NaN);
    if (Number.isFinite(value)) last = value;
    out.push(last);
  }
  return out.filter(Number.isFinite);
}

function newIdempotencyKey(): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  return typeof uuid === "string" && uuid.length > 0 ? uuid : `tick-${Date.now()}`;
}

function tapeSide(frame: StreamFrame): "BUY" | "SELL" {
  const data = frame.data;
  const price = safeNumber(data.price, safeNumber(data.price_micros, Number.NaN) / 1_000_000);
  const bid = safeNumber(data.bid, Number.NaN);
  const ask = safeNumber(data.ask, Number.NaN);
  if (Number.isFinite(price) && Number.isFinite(bid) && Number.isFinite(ask)) {
    return price >= (bid + ask) / 2 ? "BUY" : "SELL";
  }
  const side = text(data.side, "").toUpperCase();
  if (side.startsWith("B")) return "BUY";
  if (side.startsWith("S")) return "SELL";
  return frame.id % 2 === 0 ? "BUY" : "SELL";
}

export default function TerminalPage({ mode }: PageProps) {
  const [instruments, setInstruments] = useState<Record<string, unknown>[]>([]);
  const [search, setSearch] = useState("");
  const [symbol, setSymbol] = useState("SPY");
  const [timeframe, setTimeframe] = useState<string>("1m");
  const [candles, setCandles] = useState<Candle[]>([]);
  const [quote, setQuote] = useState<Record<string, unknown>>({});
  const [bids, setBids] = useState<DepthLevel[]>([]);
  const [asks, setAsks] = useState<DepthLevel[]>([]);
  const [study, setStudy] = useState<Record<string, unknown>>({});
  const [script, setScript] = useState(STUDY_IDS[0]);
  const [studiesOpen, setStudiesOpen] = useState(true);
  const [show, setShow] = useState({ ma5: true, ma10: true, ma20: false, vwap: true });
  const [ticket, setTicket] = useState({ side: "BUY", quantity: "1", type: "MARKET", limit: "" });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<Note | null>(null);

  const streamSilo = useMemo(() => {
    const row = instruments.find((entry) => text(entry.symbol) === symbol);
    return text(row?.silo, "US_IBKR");
  }, [instruments, symbol]);

  const tapeFrames = useMarketStream({ topics: ["TICK_UPDATE"], silo: streamSilo, max: 120 });

  useEffect(() => {
    let active = true;
    const loadUniverse = async () => {
      try {
        const payload = await api.get<unknown>("/instruments", { limit: 500, offset: 0 });
        if (!active) return;
        const rows = asRows(asRecord(payload).items);
        setInstruments(rows);
        const first = text(rows[0]?.symbol, "");
        if (first.length > 0) setSymbol((current) => (current === "SPY" ? first : current));
      } catch (caught) {
        if (active) setError(errorMessage(caught));
      }
    };
    void loadUniverse();
    return () => {
      active = false;
    };
  }, []);

  const loadChart = useCallback(async () => {
    const tvToken = TIMEFRAMES.find((entry) => entry.id === timeframe)?.tv ?? "1";
    try {
      const payload = await api.get<unknown>("/market/tv-chart", {
        symbol,
        timeframe: tvToken,
        bars: BAR_LIMIT,
      });
      const rows = asRows(asRecord(payload).bars).map(candleFromTv);
      const usable = rows.filter((row): row is Candle => row !== null);
      if (usable.length === 0) throw new Error("TV_CHART_EMPTY");
      setCandles(usable);
      setError(null);
      return;
    } catch {
      /* fall through to the Ring 2 bar aggregator */
    }
    try {
      const payload = await api.get<unknown>("/market/bars", {
        silo: streamSilo,
        symbol,
        timeframe,
        limit: BAR_LIMIT,
      });
      const rows = asRows(asRecord(payload).bars).map(candleFromBar);
      setCandles(rows.filter((row): row is Candle => row !== null));
      setError(null);
    } catch (caught) {
      setCandles([]);
      setError(errorMessage(caught));
    }
  }, [symbol, timeframe, streamSilo]);

  const loadQuote = useCallback(async () => {
    try {
      const posted = await api.post<unknown>("/control/terminal/quote", {
        symbol,
        silo: streamSilo,
        bars: BAR_LIMIT,
        depth: 10,
      });
      setQuote(asRecord(posted));
    } catch (caught) {
      setQuote({});
      setNote({ tone: "bear", message: `quote: ${errorMessage(caught)}` });
    }
  }, [symbol, streamSilo]);

  const loadDepth = useCallback(async () => {
    try {
      const payload = await api.get<unknown>("/market/depth", { silo: streamSilo, symbol });
      const record = asRecord(payload);
      setBids(levelRows(record.bids, true));
      setAsks(levelRows(record.asks, true));
    } catch {
      setBids([]);
      setAsks([]);
    }
  }, [symbol, streamSilo]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    const run = async () => {
      await Promise.all([loadChart(), loadQuote(), loadDepth()]);
      if (active) setLoading(false);
    };
    void run();
    const timer = globalThis.setInterval(() => void loadDepth(), 15_000);
    return () => {
      active = false;
      globalThis.clearInterval(timer);
    };
  }, [loadChart, loadQuote, loadDepth]);

  const filtered = useMemo(() => {
    const needle = search.trim().toUpperCase();
    const rows =
      needle.length === 0
        ? instruments
        : instruments.filter((row) =>
            `${text(row.symbol)} ${text(row.display_name)}`.toUpperCase().includes(needle),
          );
    return rows.slice(0, MAX_ROWS);
  }, [instruments, search]);

  const closes = useMemo(() => candles.map((candle) => candle.c), [candles]);
  const overlays = useMemo<CandleOverlays>(() => {
    const next: CandleOverlays = {};
    if (show.ma5) next.ma5 = movingAverage(closes, 5);
    if (show.ma10) next.ma10 = movingAverage(closes, 10);
    if (show.ma20) next.ma20 = movingAverage(closes, 20);
    if (show.vwap) next.vwap = vwapSeries(candles);
    return next;
  }, [closes, candles, show]);

  const quoteRecord = asRecord(quote.quote);
  const contract = asRecord(quote.contract);
  const last = safeNumber(quoteRecord.last, closes.length > 0 ? closes[closes.length - 1] : Number.NaN);
  const mid = safeNumber(quoteRecord.mid, last);
  const spreadBps = safeNumber(quoteRecord.spread_bps, 0);
  const halfSpread = Number.isFinite(mid) ? (mid * spreadBps) / 20_000 : 0;
  const frameBids = levelRows(asRecord(asRecord(quote.depth).bids), false);
  const frameAsks = levelRows(asRecord(asRecord(quote.depth).asks), false);
  const ladderBids = bids.length > 0 ? bids : frameBids;
  const ladderAsks = asks.length > 0 ? asks : frameAsks;
  const bestBid = ladderBids[0]?.price ?? (Number.isFinite(mid) ? mid - halfSpread : Number.NaN);
  const bestAsk = ladderAsks[0]?.price ?? (Number.isFinite(mid) ? mid + halfSpread : Number.NaN);
  const liveSpreadBps =
    Number.isFinite(bestBid) && Number.isFinite(bestAsk) && mid > 0
      ? ((bestAsk - bestBid) / mid) * 10_000
      : spreadBps;
  const tickSizeMicros = safeNumber(contract.tick_size_micros, 0);
  const selected = instruments.find((row) => text(row.symbol) === symbol);
  const tape = useMemo(() => [...tapeFrames.frames].reverse().slice(0, 40), [tapeFrames.frames]);

  const submitStudy = async () => {
    const studyId = script.trim().slice(0, 96).length > 0 ? script.trim().slice(0, 96) : STUDY_IDS[0];
    try {
      const payload = await api.post<unknown>("/market/tv-study", {
        symbol,
        timeframe: TIMEFRAMES.find((entry) => entry.id === timeframe)?.tv ?? "1",
        study_id: studyId,
        script,
        bars: 200,
      });
      setStudy(asRecord(payload));
      setNote({
        tone: "bull",
        message: `Study ${text(asRecord(payload).study_title, studyId)} evaluated on ${symbol}.`,
      });
    } catch (caught) {
      setStudy({});
      setNote({ tone: "bear", message: `study: ${errorMessage(caught)}` });
    }
  };

  const submitOrder = async () => {
    try {
      const quantity = assertFinite(ticket.quantity, "quantity");
      const limit = ticket.type === "MARKET" ? undefined : assertFinite(ticket.limit, "limit price");
      const order = await api.post<Record<string, unknown>>("/control/orders/paper", {
        silo: streamSilo,
        symbol,
        side: ticket.side,
        quantity,
        order_type: ticket.type,
        order_style: ticket.type === "MARKET" ? "MAKER_PEG" : "TWAP_ICEBERG",
        limit_price: limit,
        fill_immediately: ticket.type === "MARKET",
        idempotency_key: newIdempotencyKey(),
      });
      setNote({
        tone: "bull",
        message: `${text(order.status, "SUBMITTED")} · order ${text(order.order_id)} · ${ticket.side} ${quantity} ${symbol} @ ${num(order.fill_price, 4)}`,
      });
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    }
  };

  const studySeries = useMemo<Series[]>(() => {
    return Object.entries(asRecord(study.plots))
      .slice(0, 4)
      .map(([name, values]) => ({ name, points: studyPoints(values) }))
      .filter((entry) => entry.points.length > 1);
  }, [study]);

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="TERMINAL"
        subtitle="274-symbol tape, candles, Pine studies, 10-level L2 ladder and the quick order ticket"
        badges={
          <div className="flex items-center gap-1.5">
            <Badge tone={mode === "LIVE" ? "bear" : "accent"} mono>
              {mode}
            </Badge>
            <Badge tone="neutral" mono>
              {symbol}
            </Badge>
          </div>
        }
        actions={
          <Button size="sm" variant="subtle" icon="refresh" onClick={() => void loadChart()}>
            Reload bars
          </Button>
        }
      />

      {error !== null ? <Banner tone="bear">{error}</Banner> : null}
      {note !== null ? <Banner tone={note.tone}>{note.message}</Banner> : null}

      <div className="grid gap-3 xl:grid-cols-[240px_minmax(0,1fr)]">
        <Card
          title="Universe"
          subtitle={`${instruments.length} contracts`}
          dense
          actions={
            <Badge tone="muted" mono>
              {filtered.length}/{MAX_ROWS}
            </Badge>
          }
        >
          <div className="border-b border-hairline p-2">
            <TextInput
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="search symbol or name"
            />
          </div>
          <div className="nios-scroll max-h-[420px]">
            {filtered.length === 0 ? (
              <div className="p-3">
                <EmptyState
                  title="No instrument"
                  hint="The catalog filter returned nothing for this search."
                />
              </div>
            ) : (
              filtered.map((row) => {
                const rowSymbol = text(row.symbol, "");
                const active = rowSymbol === symbol;
                return (
                  <button
                    key={rowSymbol}
                    type="button"
                    onClick={() => setSymbol(rowSymbol)}
                    className={`flex w-full items-baseline justify-between gap-2 border-b border-hairline/70 px-2 py-1 text-left text-[11px] hover:bg-elevated ${active ? "bg-bull-tint/40 text-bull" : "text-ink-2"}`}
                  >
                    <span className="font-mono text-[10.5px]">{rowSymbol}</span>
                    <span className="truncate text-[9.5px] uppercase tracking-[0.08em] text-ink-3">
                      {text(row.silo, "—")}
                    </span>
                  </button>
                );
              })
            )}
          </div>
        </Card>

        <div className="flex min-w-0 flex-col gap-3">
          <Card
            title="Quote Strip"
            subtitle={`${text(contract.display_name, symbol)} · ${text(contract.venue, "venue")} · ${text(contract.currency, "—")}`}
            icon="activity"
          >
            <StatGrid columns={6}>
              <Stat label="Last" value={num(last, 4)} tone="neutral" size="sm" />
              <Stat label="Bid" value={num(bestBid, 4)} tone="bull" size="sm" />
              <Stat label="Ask" value={num(bestAsk, 4)} tone="bear" size="sm" />
              <Stat
                label="Chg"
                value={`${num(safeNumber(quoteRecord.session_change_bps, 0) / 100, 2)}%`}
                tone={safeNumber(quoteRecord.session_change_bps, 0) >= 0 ? "bull" : "bear"}
                size="sm"
              />
              <Stat label="Spread" value={`${num(liveSpreadBps, 2)} bps`} size="sm" />
              <Stat
                label="Tick"
                value={tickSizeMicros > 0 ? priceMicros(tickSizeMicros, 4) : "—"}
                size="sm"
              />
            </StatGrid>
          </Card>

          <Card
            title="Candles"
            subtitle={`${candles.length} bars · ${timeframe} · ring 2 charting bridge`}
            icon="candles"
            actions={
              <div className="flex items-center gap-1">
                {(["ma5", "ma10", "ma20", "vwap"] as const).map((key) => (
                  <Button
                    key={key}
                    size="sm"
                    variant="ghost"
                    active={show[key]}
                    onClick={() => setShow((current) => ({ ...current, [key]: !current[key] }))}
                  >
                    {key.toUpperCase()}
                  </Button>
                ))}
              </div>
            }
          >
            <Tabs
              tabs={TIMEFRAMES.map((entry) => ({ id: entry.id, label: entry.label }))}
              active={timeframe}
              onChange={setTimeframe}
            />
            <div className="mt-2">
              {loading && candles.length === 0 ? (
                <div className="flex items-center gap-2 py-6 text-[11px] text-ink-3">
                  <Spinner /> loading bars
                </div>
              ) : (
                <CandlestickChart candles={candles} overlays={overlays} height={300} width={760} />
              )}
            </div>
          </Card>

          <Card
            title="Pine Study Overlay"
            subtitle="POST /market/tv-study · grounded plot series"
            icon="sigma"
            actions={
              <div className="flex items-center gap-1.5">
                <Button
                  size="sm"
                  variant="ghost"
                  active={studiesOpen}
                  title={studiesOpen ? "Collapse the Pine studies panel" : "Expand the Pine studies panel"}
                  onClick={() => setStudiesOpen((current) => !current)}
                >
                  Pine Studies
                </Button>
                <Badge tone={studySeries.length > 0 ? "bull" : "muted"} mono>
                  {text(study.study_id, "NO STUDY")}
                </Badge>
              </div>
            }
          >
            {studiesOpen ? (
              <>
                <div className="grid gap-2 md:grid-cols-[minmax(0,1fr)_220px]">
                  <Field label="Study script / id" hint={`supported: ${STUDY_IDS.slice(0, 3).join(" · ")} …`}>
                    <TextArea rows={3} value={script} onChange={setScript} placeholder="STD;RSI" />
                  </Field>
                  <div className="flex flex-col justify-end gap-1.5">
                    <Button size="sm" variant="primary" onClick={() => void submitStudy()}>
                      Evaluate study
                    </Button>
                    <div className="text-[10px] leading-snug text-ink-3">
                      {text(asRecord(study.verdict).reason, "plots are hashed before they may be quoted")}
                    </div>
                  </div>
                </div>
                <div className="mt-2">
                  {studySeries.length === 0 ? (
                    <EmptyState
                      title="No study plotted"
                      hint="Evaluate a Pine study to render its grounded plot series."
                    />
                  ) : (
                    <LineChart series={studySeries} height={140} width={720} />
                  )}
                </div>
              </>
            ) : (
              <div className="px-1 py-3 text-[10px] uppercase tracking-[0.1em] text-ink-3">
                Pine studies panel collapsed · click PINE STUDIES to expand
              </div>
            )}
          </Card>
        </div>
      </div>

      <div className="grid gap-3 xl:grid-cols-3">
        <Card
          title="L2 Ladder"
          subtitle={`10 levels · ${text(asRecord(quote.depth).imbalance_regime, "BALANCED")}`}
          icon="layer"
          actions={
            <Badge tone="neutral" mono>
              {num(liveSpreadBps, 2)} bps
            </Badge>
          }
        >
          {ladderBids.length === 0 && ladderAsks.length === 0 ? (
            <EmptyState
              title="No depth"
              hint="The bar aggregator has not received L2 ticks for this instrument yet."
            />
          ) : (
            <DepthLadder
              bids={ladderBids.slice(0, 10)}
              asks={ladderAsks.slice(0, 10)}
              mid={mid}
              tickSizeMicros={tickSizeMicros > 0 ? tickSizeMicros : undefined}
              height={252}
              width={330}
            />
          )}
          <div className="mt-2 text-[10px] leading-snug text-ink-3">
            Prices snap to{" "}
            {tickSizeMicros > 0
              ? `${priceMicros(tickSizeMicros, 4)} (${tickSizeMicros} µ)`
              : "the venue tick"}{" "}
            · lot {num(contract.lot_size, 4)} · {bids.length > 0 ? "aggregator book" : "quote-frame snapshot"}
          </div>
        </Card>

        <Card
          title="Tape"
          subtitle="last 40 prints · Lee-Ready classification"
          icon="terminal"
          actions={
            <Badge tone={tapeFrames.status === "open" ? "bull" : "muted"} mono>
              {tapeFrames.frames.length}
            </Badge>
          }
        >
          {tape.length === 0 ? (
            <EmptyState
              title="No prints"
              hint="TICK_UPDATE frames are multiplexed on /api/v2/stream as the tape warms."
            />
          ) : (
            <div className="nios-scroll max-h-[300px]">
              <Table columns={["TIME", "PRICE", "SIZE", "SIDE"]} dense>
                {tape.map((frame) => {
                  const side = tapeSide(frame);
                  const price = safeNumber(
                    frame.data.price,
                    safeNumber(frame.data.price_micros, Number.NaN) / 1_000_000,
                  );
                  const size = safeNumber(frame.data.volume_delta, safeNumber(frame.data.size, 0));
                  return (
                    <Row key={frame.id}>
                      <Cell mono>{clockNs(frame.receivedAt * 1_000_000)}</Cell>
                      <Cell align="right" tone={side === "BUY" ? "bull" : "bear"}>
                        {num(price, 4)}
                      </Cell>
                      <Cell align="right">{qty(size, 0)}</Cell>
                      <Cell align="right" mono tone={side === "BUY" ? "bull" : "bear"}>
                        {side}
                      </Cell>
                    </Row>
                  );
                })}
              </Table>
            </div>
          )}
        </Card>

        <Card title="Quick Ticket" subtitle="paper execution · grounded proposal" icon="orders">
          <div className="grid grid-cols-2 gap-2">
            <Field label="Side">
              <SelectInput
                value={ticket.side}
                onChange={(value) => setTicket((current) => ({ ...current, side: value }))}
                options={SIDE_OPTIONS}
              />
            </Field>
            <Field label="Quantity">
              <TextInput
                mono
                value={ticket.quantity}
                onChange={(event) => setTicket((current) => ({ ...current, quantity: event.target.value }))}
              />
            </Field>
            <Field label="Type">
              <SelectInput
                value={ticket.type}
                onChange={(value) => setTicket((current) => ({ ...current, type: value }))}
                options={TYPE_OPTIONS}
              />
            </Field>
            <Field label="Limit price" hint={ticket.type === "MARKET" ? "market sweep" : "resting peg"}>
              <TextInput
                mono
                value={ticket.limit}
                disabled={ticket.type === "MARKET"}
                onChange={(event) => setTicket((current) => ({ ...current, limit: event.target.value }))}
                placeholder={num(mid, 4)}
              />
            </Field>
          </div>
          <div className="mt-2 flex items-center justify-between gap-2">
            <div className="text-[10px] uppercase tracking-[0.08em] text-ink-3">
              {streamSilo} · {text(selected?.asset_class, "—")}
            </div>
            <Button
              size="sm"
              variant={ticket.side === "BUY" ? "success" : "danger"}
              onClick={() => void submitOrder()}
            >
              Submit paper order
            </Button>
          </div>
          <div className="mt-2 border-t border-hairline pt-2">
            <KeyValue
              columns={2}
              items={[
                {
                  label: "Notional / lot",
                  value: usd(
                    safeNumber(contract.lot_size, 1) * (Number.isFinite(last) ? last : 0) * 1_000_000,
                  ),
                },
                {
                  label: "Circuit band",
                  value: Number.isFinite(safeNumber(asRecord(contract.circuit_band).lower, Number.NaN))
                    ? `${num(asRecord(contract.circuit_band).lower, 2)} / ${num(asRecord(contract.circuit_band).upper, 2)}`
                    : "—",
                },
                ...SILOS.filter((silo) => silo.id === streamSilo).map((silo) => ({
                  label: "Tax floor",
                  value: `${num(silo.floorBps, 2)} bps`,
                })),
              ]}
            />
          </div>
        </Card>
      </div>
    </div>
  );
}

export { TerminalPage };
