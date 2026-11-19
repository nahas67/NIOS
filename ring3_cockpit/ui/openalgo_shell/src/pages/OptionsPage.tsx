import { useEffect, useMemo, useState } from "react";
import { PayoffCurveChart, type PayoffPoint } from "../charts";
import {
  Badge,
  Banner,
  Button,
  Card,
  Cell,
  EmptyState,
  Field,
  PageHeader,
  Row,
  SelectInput,
  Spinner,
  Stat,
  StatGrid,
  Table,
  TextInput,
  cx,
} from "../components/ui";
import { api, assertFinite, errorMessage } from "../lib/api";
import { asArray, asRecord, compact, num, safeBool, safeNumber, text } from "../lib/format";
import { rowsOf } from "../lib/payload";
import { IvRankStat } from "../components/telemetry";
import { useTicker } from "../state/session";
import type { ApiNote, PageProps } from "./types";

type Side = "BUY" | "SELL";
type OptionType = "CALL" | "PUT";

interface LegDraft {
  side: Side;
  type: OptionType;
  strike: string;
  qty: string;
  premium: string;
}

interface ChainRow {
  strike: number;
  callOi: number;
  callIv: number;
  callLtp: number;
  putLtp: number;
  putIv: number;
  putOi: number;
}

interface PayoffResult {
  points: PayoffPoint[];
  breakevens: number[];
  maxProfit: number;
  maxLoss: number;
}

const FALLBACK_INSTRUMENTS = [
  "NIFTY",
  "BANKNIFTY",
  "FINNIFTY",
  "MIDCPNIFTY",
  "RELIANCE",
  "HDFCBANK",
  "INFY",
  "TCS",
];

const PRESETS: Array<{ id: string; label: string }> = [
  { id: "LONG_CALL", label: "LONG CALL" },
  { id: "LONG_PUT", label: "LONG PUT" },
  { id: "BULL_CALL", label: "BULL CALL SPREAD" },
  { id: "BEAR_PUT", label: "BEAR PUT SPREAD" },
  { id: "IRON_CONDOR", label: "IRON CONDOR" },
  { id: "STRADDLE", label: "STRADDLE" },
];

function chainOf(payload: unknown): ChainRow[] {
  return rowsOf(payload)
    .map((row) => {
      const call = asRecord(row.call ?? row.ce ?? row.call_side);
      const put = asRecord(row.put ?? row.pe ?? row.put_side);
      return {
        strike: safeNumber(row.strike ?? row.strike_price ?? row.strikePrice, 0),
        callOi: safeNumber(row.call_oi ?? row.ce_oi ?? call.oi ?? call.open_interest, 0),
        callIv: safeNumber(row.call_iv ?? row.ce_iv ?? call.iv, Number.NaN),
        callLtp: safeNumber(row.call_ltp ?? row.ce_ltp ?? call.ltp ?? call.last_price, 0),
        putLtp: safeNumber(row.put_ltp ?? row.pe_ltp ?? put.ltp ?? put.last_price, 0),
        putIv: safeNumber(row.put_iv ?? row.pe_iv ?? put.iv, Number.NaN),
        putOi: safeNumber(row.put_oi ?? row.pe_oi ?? put.oi ?? put.open_interest, 0),
      };
    })
    .filter((row) => row.strike > 0)
    .sort((left, right) => left.strike - right.strike);
}

function maxPainStrike(rows: ChainRow[]): number {
  let best = 0;
  let bestPain = Number.POSITIVE_INFINITY;
  for (const candidate of rows) {
    let pain = 0;
    for (const row of rows) {
      if (candidate.strike > row.strike) pain += (candidate.strike - row.strike) * row.callOi;
      if (candidate.strike < row.strike) pain += (row.strike - candidate.strike) * row.putOi;
    }
    if (pain < bestPain) {
      bestPain = pain;
      best = candidate.strike;
    }
  }
  return best;
}

function computePayoff(legs: LegDraft[], underlying: number): PayoffResult | null {
  const parsed = legs
    .map((leg) => ({
      side: leg.side,
      type: leg.type,
      strike: safeNumber(leg.strike, 0),
      qty: safeNumber(leg.qty, 0),
      premium: safeNumber(leg.premium, 0),
    }))
    .filter((leg) => leg.strike > 0 && leg.qty > 0);
  if (parsed.length === 0 || underlying <= 0) return null;
  const points: PayoffPoint[] = [];
  const steps = 60;
  for (let index = 0; index <= steps; index += 1) {
    const spot = underlying * (0.75 + (0.5 * index) / steps);
    let pnl = 0;
    for (const leg of parsed) {
      const intrinsic = leg.type === "CALL" ? Math.max(0, spot - leg.strike) : Math.max(0, leg.strike - spot);
      const sign = leg.side === "BUY" ? 1 : -1;
      pnl += sign * (intrinsic - leg.premium) * leg.qty;
    }
    points.push({ spot: Number(spot.toFixed(4)), pnl: Number(pnl.toFixed(4)) });
  }
  const breakevens: number[] = [];
  for (let index = 1; index < points.length; index += 1) {
    const previous = points[index - 1];
    const current = points[index];
    if (previous.pnl === 0) {
      breakevens.push(previous.spot);
      continue;
    }
    if (previous.pnl * current.pnl < 0) {
      const span = current.spot - previous.spot;
      const slope = (current.pnl - previous.pnl) / (span || 1);
      breakevens.push(Number((previous.spot - previous.pnl / (slope || 1)).toFixed(2)));
    }
  }
  const pnls = points.map((point) => point.pnl);
  return {
    points,
    breakevens,
    maxProfit: Math.max(...pnls),
    maxLoss: Math.min(...pnls),
  };
}

function presetLegs(
  id: string,
  atm: number,
  step: number,
  priceOf: (strike: number, type: OptionType) => number,
): LegDraft[] {
  const leg = (side: Side, type: OptionType, strike: number): LegDraft => ({
    side,
    type,
    strike: String(strike),
    qty: "1",
    premium: String(priceOf(strike, type)),
  });
  switch (id) {
    case "LONG_CALL":
      return [leg("BUY", "CALL", atm)];
    case "LONG_PUT":
      return [leg("BUY", "PUT", atm)];
    case "BULL_CALL":
      return [leg("BUY", "CALL", atm), leg("SELL", "CALL", atm + step)];
    case "BEAR_PUT":
      return [leg("BUY", "PUT", atm), leg("SELL", "PUT", atm - step)];
    case "IRON_CONDOR":
      return [
        leg("SELL", "PUT", atm - step),
        leg("BUY", "PUT", atm - 2 * step),
        leg("SELL", "CALL", atm + step),
        leg("BUY", "CALL", atm + 2 * step),
      ];
    case "STRADDLE":
      return [leg("BUY", "CALL", atm), leg("BUY", "PUT", atm)];
    default:
      return [];
  }
}

export default function OptionsPage({ mode }: PageProps) {
  const now = useTicker(1000);
  const [instruments, setInstruments] = useState<string[]>(FALLBACK_INSTRUMENTS);
  const [symbol, setSymbol] = useState("NIFTY");
  const [strikeCount, setStrikeCount] = useState("9");
  const [dte, setDte] = useState("7");
  const [chainPayload, setChainPayload] = useState<unknown>({});
  const [chainRows, setChainRows] = useState<ChainRow[]>([]);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<ApiNote | null>(null);
  const [legs, setLegs] = useState<LegDraft[]>([]);
  const [priced, setPriced] = useState<{ source: "BACKEND" | "CLIENT"; result: PayoffResult } | null>(null);

  useEffect(() => {
    let live = true;
    const run = async () => {
      try {
        const payload = await api.get<unknown>("/instruments", { options: true });
        const rows = rowsOf(payload);
        const flagged = rows.filter((row) => safeBool(row.optionable, false));
        const pool = flagged.length > 0 ? flagged : rows;
        const symbols = pool
          .map((row) => text(row.symbol ?? row.ticker ?? row.code, ""))
          .filter((entry) => entry.length > 0);
        if (live && symbols.length > 0) setInstruments(Array.from(new Set(symbols)).slice(0, 147));
      } catch (err) {
        if (live) setNote({ tone: "neutral", message: `instrument picker degraded: ${errorMessage(err)}` });
      }
    };
    void run();
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    let live = true;
    const run = async () => {
      setLoading(true);
      setError(null);
      try {
        const count = Math.min(31, Math.max(3, Math.trunc(assertFinite(strikeCount, "strike_count"))));
        const expiry = Math.min(365, Math.max(1, Math.trunc(assertFinite(dte, "days_to_expiry"))));
        const payload = await api.post<unknown>("/control/options/chain", {
          symbol,
          strike_count: count,
          days_to_expiry: expiry,
        });
        if (!live) return;
        setChainPayload(payload);
        setChainRows(chainOf(payload));
      } catch (err) {
        if (!live) return;
        setChainPayload({});
        setChainRows([]);
        setError(errorMessage(err));
      } finally {
        if (live) setLoading(false);
      }
    };
    void run();
    return () => {
      live = false;
    };
  }, [symbol, reloadNonce]);

  const record = asRecord(chainPayload);
  const spot = safeNumber(record.spot ?? record.underlying_ltp ?? record.ltp, 0);
  const explicitAtm = safeNumber(record.atm_strike, 0);
  const atm = useMemo(() => {
    if (explicitAtm > 0) return explicitAtm;
    if (chainRows.length === 0) return 0;
    const target = spot > 0 ? spot : chainRows[Math.floor(chainRows.length / 2)].strike;
    return chainRows.reduce((best, row) => (Math.abs(row.strike - target) < Math.abs(best - target) ? row.strike : best), chainRows[0].strike);
  }, [chainRows, explicitAtm, spot]);
  const underlying = spot > 0 ? spot : atm;
  const step = useMemo(() => {
    if (chainRows.length < 2) return Math.max(1, Math.round(underlying * 0.01));
    return Math.max(1, Math.round(chainRows[1].strike - chainRows[0].strike));
  }, [chainRows, underlying]);

  const callOi = chainRows.reduce((total, row) => total + row.callOi, 0);
  const putOi = chainRows.reduce((total, row) => total + row.putOi, 0);
  const backendPcr = safeNumber(record.pcr ?? record.put_call_ratio, Number.NaN);
  const pcr = Number.isFinite(backendPcr) ? backendPcr : callOi > 0 ? putOi / callOi : Number.NaN;
  const backendMaxPain = safeNumber(record.max_pain, Number.NaN);
  const computedMaxPain = useMemo(() => maxPainStrike(chainRows), [chainRows]);
  const maxPain = Number.isFinite(backendMaxPain) ? backendMaxPain : computedMaxPain;
  // V2.3 emits a deterministic `iv_rank_bps` (0…10000) on the chain response.
  const ivRankBpsRaw = safeNumber(record.iv_rank_bps, Number.NaN);
  const ivRankBps = Number.isFinite(ivRankBpsRaw) ? ivRankBpsRaw : null;
  const legacyIvRankRaw = safeNumber(record.iv_rank ?? record.iv_rank_pct, Number.NaN);
  const legacyIvRank = Number.isFinite(legacyIvRankRaw) ? legacyIvRankRaw : null;
  const gex = safeNumber(record.gex ?? record.net_gex, Number.NaN);

  const priceOf = (strike: number, type: OptionType): number => {
    const row = chainRows.find((entry) => entry.strike === strike);
    const fromChain = row ? (type === "CALL" ? row.callLtp : row.putLtp) : 0;
    if (fromChain > 0) return Number(fromChain.toFixed(2));
    return Number(Math.max(0.05, underlying * 0.012).toFixed(2));
  };

  function applyPreset(id: string) {
    if (atm <= 0) {
      setNote({ tone: "bear", message: "Load a chain before seeding a strategy." });
      return;
    }
    setLegs(presetLegs(id, atm, step, priceOf));
    setPriced(null);
  }

  function updateLeg(index: number, patch: Partial<LegDraft>) {
    setLegs((previous) => previous.map((leg, position) => (position === index ? { ...leg, ...patch } : leg)));
    setPriced(null);
  }

  const clientPayoff = useMemo(() => computePayoff(legs, underlying), [legs, underlying]);

  async function priceWithBackend() {
    try {
      const parsed = legs.map((leg) => ({
        side: leg.side,
        option_type: leg.type,
        strike: assertFinite(leg.strike, "leg strike"),
        quantity: assertFinite(leg.qty, "leg quantity"),
        premium: assertFinite(leg.premium, "leg premium"),
      }));
      if (parsed.length === 0) throw new Error("add at least one leg before pricing");
      if (parsed.some((leg) => leg.strike <= 0 || leg.quantity <= 0 || leg.premium < 0)) {
        throw new Error("every leg needs strike > 0, quantity > 0 and premium >= 0");
      }
      const payload = await api.post<unknown>("/control/options/strategy-payoff", {
        symbol,
        spot: underlying,
        legs: parsed,
      });
      const body = asRecord(payload);
      const maxProfit = safeNumber(body.max_profit ?? body.maxProfit, Number.NaN);
      const maxLoss = safeNumber(body.max_loss ?? body.maxLoss, Number.NaN);
      const breakevens = asArray(body.breakevens)
        .map((entry) => {
          if (typeof entry === "number") return entry;
          const item = asRecord(entry);
          return safeNumber(item.breakeven ?? item.value ?? item.strike, Number.NaN);
        })
        .filter((value) => Number.isFinite(value));
      if (!Number.isFinite(maxProfit) || !Number.isFinite(maxLoss) || !clientPayoff) {
        throw new Error("backend returned no payoff envelope");
      }
      setPriced({
        source: "BACKEND",
        result: { points: clientPayoff.points, breakevens: breakevens.length > 0 ? breakevens : clientPayoff.breakevens, maxProfit, maxLoss },
      });
      setNote({ tone: "bull", message: `Payoff priced by backend for ${symbol}` });
    } catch (err) {
      if (clientPayoff) {
        setPriced({ source: "CLIENT", result: clientPayoff });
        setNote({ tone: "neutral", message: `backend payoff unavailable — client mirror shown (${errorMessage(err)})` });
      } else {
        setNote({ tone: "bear", message: errorMessage(err) });
      }
    }
  }

  const shown = priced?.result ?? clientPayoff;
  const sourceLabel = priced?.source ?? (clientPayoff ? "CLIENT" : "—");

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="OPTIONS"
        subtitle="Universal chain across 147 optionable instruments · GEX · payoff builder"
        badges={
          <div className="flex items-center gap-1">
            <Badge tone={mode === "LIVE" ? "bear" : "accent"} mono>
              {mode}
            </Badge>
            <Badge tone="muted" mono>
              PAYOFF {sourceLabel}
            </Badge>
          </div>
        }
        actions={
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] uppercase tracking-[0.1em] text-ink-3 tabular-nums">
              {new Date(now).toISOString().slice(11, 19)} UTC
            </span>
            <SelectInput
              className="w-[150px]"
              value={symbol}
              onChange={(value) => {
                setSymbol(value);
                setLegs([]);
                setPriced(null);
              }}
              options={instruments.map((entry) => ({ value: entry, label: entry }))}
            />
            <Button icon="refresh" variant="subtle" size="sm" onClick={() => setReloadNonce((value) => value + 1)}>
              Reload
            </Button>
          </div>
        }
      />
      {error ? <Banner tone="bear">{error}</Banner> : null}
      {note ? <Banner tone={note.tone}>{note.message}</Banner> : null}

      <StatGrid columns={4}>
        <IvRankStat ivRankBps={ivRankBps} legacyPercentile={legacyIvRank} />
        <Stat
          label="Net GEX"
          value={Number.isFinite(gex) ? compact(gex) : "—"}
          hint="dealer gamma exposure"
          tone={Number.isFinite(gex) ? (gex >= 0 ? "bull" : "bear") : "muted"}
        />
        <Stat
          label="PCR"
          value={Number.isFinite(pcr) ? num(pcr, 3) : "—"}
          hint={callOi > 0 ? "put OI / call OI" : "chain empty"}
          tone={Number.isFinite(pcr) && pcr > 1.2 ? "bear" : "neutral"}
        />
        <Stat
          label="Max Pain"
          value={maxPain > 0 ? num(maxPain, 2) : "—"}
          hint={Number.isFinite(backendMaxPain) ? "backend" : "computed"}
          tone="accent"
        />
      </StatGrid>

      <Card
        title="Option Chain"
        subtitle={`${symbol} · ${chainRows.length} strikes · ${mode === "LIVE" ? "live marks" : "paper marks"}`}
        actions={
          <div className="flex items-center gap-1.5">
            <span className="text-[9.5px] uppercase tracking-[0.1em] text-ink-3">Strikes</span>
            <TextInput
              className="w-[52px] font-mono"
              value={strikeCount}
              onChange={(event) => setStrikeCount(event.target.value)}
            />
            <span className="text-[9.5px] uppercase tracking-[0.1em] text-ink-3">DTE</span>
            <TextInput className="w-[46px] font-mono" value={dte} onChange={(event) => setDte(event.target.value)} />
            <Button size="sm" variant="subtle" icon="refresh" onClick={() => setReloadNonce((value) => value + 1)}>
              Load
            </Button>
          </div>
        }
        dense
      >
        {loading ? (
          <div className="flex items-center gap-2 p-3 text-[11px] text-ink-3">
            <Spinner /> loading chain
          </div>
        ) : chainRows.length === 0 ? (
          <EmptyState title="No chain" hint="Adjust the symbol, strike count or expiry and reload." />
        ) : (
          <Table columns={["Call OI", "Call IV", "Call LTP", "Strike", "Put LTP", "Put IV", "Put OI"]}>
            {chainRows.map((row) => {
              const isAtm = atm > 0 && row.strike === atm;
              const callItm = atm > 0 && row.strike < atm;
              const putItm = atm > 0 && row.strike > atm;
              return (
                <Row
                  key={row.strike}
                  className={cx(isAtm && "bg-elevated", callItm && "bg-bull-tint", putItm && "bg-bear-tint")}
                >
                  <Cell align="right" tone="muted">{compact(row.callOi)}</Cell>
                  <Cell align="right" tone="muted">{Number.isFinite(row.callIv) ? num(row.callIv, 2) : "—"}</Cell>
                  <Cell align="right" tone={callItm ? "bull" : "neutral"}>{num(row.callLtp, 2)}</Cell>
                  <Cell align="center" mono tone={isAtm ? "accent" : "neutral"} className={isAtm ? "font-semibold" : undefined}>
                    {num(row.strike, 2)}
                  </Cell>
                  <Cell align="left" tone={putItm ? "bear" : "neutral"}>{num(row.putLtp, 2)}</Cell>
                  <Cell align="left" tone="muted">{Number.isFinite(row.putIv) ? num(row.putIv, 2) : "—"}</Cell>
                  <Cell align="left" tone="muted">{compact(row.putOi)}</Cell>
                </Row>
              );
            })}
          </Table>
        )}
      </Card>

      <div className="grid min-w-0 grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)]">
        <Card
          title="Strategy Builder"
          subtitle="six presets · editable legs"
          actions={<Badge tone="muted">{`${legs.length} LEGS`}</Badge>}
        >
          <div className="flex flex-wrap items-center gap-1">
            {PRESETS.map((preset) => (
              <Button key={preset.id} size="sm" variant="ghost" onClick={() => applyPreset(preset.id)}>
                {preset.label}
              </Button>
            ))}
            <Button
              size="sm"
              variant="subtle"
              icon="plus"
              onClick={() =>
                setLegs((previous) => [
                  ...previous,
                  {
                    side: "BUY",
                    type: "CALL",
                    strike: String(atm || underlying || ""),
                    qty: "1",
                    premium: String(priceOf(atm, "CALL")),
                  },
                ])
              }
            >
              Leg
            </Button>
          </div>
          <div className="mt-2">
            {legs.length === 0 ? (
              <EmptyState title="No legs" hint="Pick a preset or add a leg." />
            ) : (
              <Table columns={["Side", "Type", "Strike", "Qty", "Premium", ""]} dense>
                {legs.map((leg, index) => (
                  <Row key={`${leg.side}-${leg.type}-${index}`}>
                    <Cell>
                      <SelectInput
                        className="w-[74px]"
                        value={leg.side}
                        onChange={(value) => updateLeg(index, { side: value === "SELL" ? "SELL" : "BUY" })}
                        options={[
                          { value: "BUY", label: "BUY" },
                          { value: "SELL", label: "SELL" },
                        ]}
                      />
                    </Cell>
                    <Cell>
                      <SelectInput
                        className="w-[80px]"
                        value={leg.type}
                        onChange={(value) => updateLeg(index, { type: value === "PUT" ? "PUT" : "CALL" })}
                        options={[
                          { value: "CALL", label: "CALL" },
                          { value: "PUT", label: "PUT" },
                        ]}
                      />
                    </Cell>
                    <Cell>
                      <TextInput
                        className="w-[86px] font-mono"
                        value={leg.strike}
                        onChange={(event) => updateLeg(index, { strike: event.target.value })}
                      />
                    </Cell>
                    <Cell>
                      <TextInput
                        className="w-[56px] font-mono"
                        value={leg.qty}
                        onChange={(event) => updateLeg(index, { qty: event.target.value })}
                      />
                    </Cell>
                    <Cell>
                      <TextInput
                        className="w-[76px] font-mono"
                        value={leg.premium}
                        onChange={(event) => updateLeg(index, { premium: event.target.value })}
                      />
                    </Cell>
                    <Cell align="right">
                      <Button
                        size="sm"
                        variant="ghost"
                        icon="close"
                        onClick={() => {
                          setLegs((previous) => previous.filter((_, position) => position !== index));
                          setPriced(null);
                        }}
                      >
                        Drop
                      </Button>
                    </Cell>
                  </Row>
                ))}
              </Table>
            )}
          </div>
          <div className="mt-2 flex items-center justify-between gap-2">
            <span className="text-[10px] uppercase tracking-[0.1em] text-ink-3">
              SPOT {underlying > 0 ? num(underlying, 2) : "—"} · STEP {num(step, 2)}
            </span>
            <Button size="sm" variant="primary" icon="sigma" onClick={() => void priceWithBackend()}>
              Price with backend
            </Button>
          </div>
        </Card>

        <Card
          title="Payoff Curve"
          subtitle="expiry P&L across ±25% spot"
          actions={<Badge tone={sourceLabel === "BACKEND" ? "bull" : sourceLabel === "CLIENT" ? "accent" : "muted"}>{sourceLabel}</Badge>}
        >
          <PayoffCurveChart points={shown?.points ?? []} breakevens={shown?.breakevens ?? []} height={188} />
          <div className="mt-2 grid grid-cols-3 gap-2">
            <Field label="Max Profit">
              <div className={cx("text-[13px] font-semibold tabular-nums", (shown?.maxProfit ?? 0) >= 0 ? "text-bull" : "text-bear")}>
                {shown ? num(shown.maxProfit, 2) : "—"}
              </div>
            </Field>
            <Field label="Max Loss">
              <div className="text-[13px] font-semibold text-bear tabular-nums">{shown ? num(shown.maxLoss, 2) : "—"}</div>
            </Field>
            <Field label="Breakevens">
              <div className="text-[12px] tabular-nums text-ink">
                {shown && shown.breakevens.length > 0 ? shown.breakevens.map((value) => num(value, 2)).join(" · ") : "—"}
              </div>
            </Field>
          </div>
        </Card>
      </div>
    </div>
  );
}
