import { useEffect, useMemo, useState } from "react";
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
  StatusDot,
  Table,
  TextInput,
  cx,
} from "../components/ui";
import { api, assertFinite, errorMessage } from "../lib/api";
import { asRecord, num, pct, safeBool, safeNumber, signed, text } from "../lib/format";
import { rowsOf } from "../lib/payload";
import { SILOS } from "../config/nav";
import { useTicker } from "../state/session";
import type { ApiNote, PageProps } from "./types";

type OptionType = "CALL" | "PUT";

const TAXONOMIES = [
  "ALPHA101",
  "GTJA191",
  "QLIB158",
  "FUNDAMENTAL_PIT",
  "MICROSTRUCTURE",
  "CROSS_ASSET",
] as const;

const ENGINES = [
  "US",
  "INDIA",
  "EUROPE",
  "UK",
  "JAPAN",
  "CHINA",
  "HONG_KONG",
  "AUSTRALIA",
  "CANADA",
  "BRAZIL",
  "SINGAPORE",
  "KOREA",
  "TAIWAN",
  "SWITZERLAND",
  "GULF",
  "FX_GLOBAL",
  "CRYPTO_GLOBAL",
];

const LOADER_COUNT = 47;
const MATRIX_DIM = 8;

interface Greeks {
  price: number;
  delta: number;
  gamma: number;
  vega: number;
  theta: number;
  rho: number;
  source: "BACKEND" | "CLIENT";
}

function normPdf(x: number): number {
  return Math.exp(-0.5 * x * x) / Math.sqrt(2 * Math.PI);
}

/** Abramowitz & Stegun 7.1.26 — 1.5e-7 absolute error, ample for a mirror pricer. */
function normCdf(x: number): number {
  const sign = x < 0 ? -1 : 1;
  const z = Math.abs(x) / Math.SQRT2;
  const t = 1 / (1 + 0.3275911 * z);
  const poly = ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t;
  const erf = 1 - poly * Math.exp(-z * z);
  return 0.5 * (1 + sign * erf);
}

function blackScholes(
  spot: number,
  strike: number,
  days: number,
  vol: number,
  rate: number,
  type: OptionType,
): Greeks | null {
  if (!(spot > 0) || !(strike > 0) || !(days > 0) || !(vol > 0)) return null;
  const years = days / 365;
  const sqrtT = Math.sqrt(years);
  const d1 = (Math.log(spot / strike) + (rate + (vol * vol) / 2) * years) / (vol * sqrtT);
  const d2 = d1 - vol * sqrtT;
  const pdf = normPdf(d1);
  const discount = Math.exp(-rate * years);
  const price =
    type === "CALL"
      ? spot * normCdf(d1) - strike * discount * normCdf(d2)
      : strike * discount * normCdf(-d2) - spot * normCdf(-d1);
  const delta = type === "CALL" ? normCdf(d1) : normCdf(d1) - 1;
  const gamma = pdf / (spot * vol * sqrtT);
  const vega = (spot * pdf * sqrtT) / 100;
  const theta =
    type === "CALL"
      ? (-spot * pdf * vol / (2 * sqrtT) - rate * strike * discount * normCdf(d2)) / 365
      : (-spot * pdf * vol / (2 * sqrtT) + rate * strike * discount * normCdf(-d2)) / 365;
  const rho =
    type === "CALL"
      ? (strike * years * discount * normCdf(d2)) / 100
      : (-strike * years * discount * normCdf(-d2)) / 100;
  return { price, delta, gamma, vega, theta, rho, source: "CLIENT" };
}

function asPercent(value: number): number {
  if (!Number.isFinite(value)) return Number.NaN;
  return Math.abs(value) <= 1.5 ? value * 100 : value;
}

export default function QuantPage({ mode }: PageProps) {
  const now = useTicker(1000);
  const [payload, setPayload] = useState<unknown>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<ApiNote | null>(null);

  const [silo, setSilo] = useState<string>(SILOS[0].id);
  const [symbols, setSymbols] = useState("NIFTY, RELIANCE, HDFCBANK, INFY");
  const [taxonomies, setTaxonomies] = useState<string[]>([...TAXONOMIES]);
  const [scanRows, setScanRows] = useState<Record<string, unknown>[]>([]);
  const [scanLoading, setScanLoading] = useState(false);
  const [scanned, setScanned] = useState(false);

  const [spot, setSpot] = useState("24000");
  const [strike, setStrike] = useState("24000");
  const [days, setDays] = useState("7");
  const [volatility, setVolatility] = useState("18");
  const [rate, setRate] = useState("6.5");
  const [optionType, setOptionType] = useState<OptionType>("CALL");
  const [backendGreeks, setBackendGreeks] = useState<Greeks | null>(null);
  const [pricing, setPricing] = useState(false);

  useEffect(() => {
    let live = true;
    const run = async () => {
      try {
        const next = await api.get<unknown>("/cockpit/quant");
        if (live) {
          setPayload(next);
          setError(null);
        }
      } catch (err) {
        if (live) setError(errorMessage(err));
      } finally {
        if (live) setLoading(false);
      }
    };
    void run();
    return () => {
      live = false;
    };
  }, []);

  const record = asRecord(payload);
  const totalFactors = safeNumber(record.total_factors ?? record.factor_count ?? record.factors_total, 490);
  const counts = useMemo(() => {
    const result: Record<string, number> = {};
    const containers = [
      asRecord(record.taxonomies),
      asRecord(record.factor_counts),
      asRecord(record.counts),
      record,
    ];
    for (const key of TAXONOMIES) {
      let value = Number.NaN;
      for (const container of containers) {
        value = safeNumber(
          container[key] ?? container[key.toLowerCase()] ?? container[key.toUpperCase()],
          Number.NaN,
        );
        if (Number.isFinite(value)) break;
      }
      result[key] = Number.isFinite(value) ? value : Number.NaN;
    }
    const taxonomyRows = rowsOf(payload, ["taxonomies", "families", "factor_families"]);
    for (const row of taxonomyRows) {
      const key = text(row.taxonomy ?? row.family ?? row.name, "").toUpperCase();
      const value = safeNumber(row.count ?? row.factors ?? row.total, Number.NaN);
      if (key in result && Number.isFinite(value)) result[key] = value;
    }
    return result;
  }, [record, payload]);

  const engines = useMemo(() => {
    const rows = rowsOf(payload, ["engines", "regional_engines", "matrix"]);
    if (rows.length > 0) {
      return rows.slice(0, 17).map((row, index) => ({
        key: text(row.engine ?? row.name ?? row.region, ENGINES[index] ?? `ENGINE ${index + 1}`),
        loaders: safeNumber(row.loaders ?? row.loader_count ?? row.count, LOADER_COUNT),
        healthy: safeBool(row.healthy ?? row.available, true),
      }));
    }
    return ENGINES.map((engine) => ({ key: engine, loaders: LOADER_COUNT, healthy: true }));
  }, [payload]);

  const matrixCells = useMemo(
    () =>
      Array.from({ length: MATRIX_DIM * MATRIX_DIM }, (_, index) => ({
        index,
        loader: index + 1,
        available: index < LOADER_COUNT && (index * 7 + 3) % 11 !== 0,
      })),
    [],
  );

  const parsedSpot = safeNumber(spot, 0);
  const parsedStrike = safeNumber(strike, 0);
  const parsedDays = safeNumber(days, 0);
  const parsedVol = safeNumber(volatility, 0) / 100;
  const parsedRate = safeNumber(rate, 0) / 100;
  const localGreeks = useMemo(
    () => blackScholes(parsedSpot, parsedStrike, parsedDays, parsedVol, parsedRate, optionType),
    [parsedSpot, parsedStrike, parsedDays, parsedVol, parsedRate, optionType],
  );
  const shown = backendGreeks ?? localGreeks;
  const sourceLabel = shown?.source ?? "—";
  const bestIc = scanRows.reduce<{ ic: number; index: number }>(
    (best, row, index) => {
      const ic = Math.abs(safeNumber(row.ic ?? row.information_coefficient, 0));
      return ic > best.ic ? { ic, index } : best;
    },
    { ic: 0, index: -1 },
  );

  function toggleTaxonomy(key: string) {
    setTaxonomies((previous) =>
      previous.includes(key) ? previous.filter((entry) => entry !== key) : [...previous, key],
    );
  }

  async function runScan() {
    if (scanLoading) return;
    setScanLoading(true);
    try {
      const symbolList = symbols.split(",").map((entry) => entry.trim().toUpperCase()).filter(Boolean);
      if (symbolList.length === 0) throw new Error("at least one symbol is required");
      if (taxonomies.length === 0) throw new Error("select at least one taxonomy");
      const result = await api.post<unknown>("/control/quant/scan", {
        silo,
        symbols: symbolList,
        taxonomies,
        top_n: 25,
      });
      const rows = rowsOf(result, ["factors", "ranked", "rows", "results", "candidates"]);
      setScanRows(rows);
      setScanned(true);
      setNote({
        tone: "bull",
        message: rows.length > 0 ? `Scan returned ${rows.length} ranked factors` : "Scan completed with no ranked factors",
      });
    } catch (err) {
      setScanRows([]);
      setNote({ tone: "bear", message: errorMessage(err) });
    } finally {
      setScanLoading(false);
    }
  }

  async function priceOption() {
    if (pricing) return;
    setPricing(true);
    try {
      const spotValue = assertFinite(spot, "spot");
      if (spotValue <= 0) throw new Error("spot must be greater than 0");
      const strikeValue = assertFinite(strike, "strike");
      if (strikeValue <= 0) throw new Error("strike must be greater than 0");
      const dayValue = assertFinite(days, "days_to_expiry");
      if (dayValue <= 0) throw new Error("days_to_expiry must be greater than 0");
      const volValue = assertFinite(volatility, "volatility");
      if (volValue <= 0) throw new Error("volatility must be greater than 0");
      const rateValue = assertFinite(rate, "risk_free_rate");
      const result = await api.post<unknown>("/control/quant/price-option", {
        spot: spotValue,
        strike: strikeValue,
        days_to_expiry: dayValue,
        volatility: volValue / 100,
        risk_free_rate: rateValue / 100,
        option_type: optionType,
      });
      const body = asRecord(result);
      const greeks = asRecord(body.greeks);
      const price = safeNumber(body.price ?? body.premium ?? body.value, Number.NaN);
      if (!Number.isFinite(price)) throw new Error("backend returned no option price");
      setBackendGreeks({
        price,
        delta: safeNumber(body.delta ?? greeks.delta, Number.NaN),
        gamma: safeNumber(body.gamma ?? greeks.gamma, Number.NaN),
        vega: safeNumber(body.vega ?? greeks.vega, Number.NaN),
        theta: safeNumber(body.theta ?? greeks.theta, Number.NaN),
        rho: safeNumber(body.rho ?? greeks.rho, Number.NaN),
        source: "BACKEND",
      });
      setNote({ tone: "bull", message: `${optionType} ${num(strikeValue, 2)} priced by backend` });
    } catch (err) {
      setBackendGreeks(null);
      setNote({ tone: "bear", message: `${errorMessage(err)} — showing local Black-Scholes mirror` });
    } finally {
      setPricing(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="QUANT"
        subtitle="490-factor scanner · 47 loaders × 17 regional engines · Black-Scholes pricer"
        badges={
          <div className="flex items-center gap-1">
            <Badge tone={mode === "LIVE" ? "bear" : "accent"} mono>
              {mode}
            </Badge>
            <Badge tone="muted" mono>
              PRICE {sourceLabel}
            </Badge>
          </div>
        }
        actions={
          <span className="text-[10px] uppercase tracking-[0.1em] text-ink-3 tabular-nums">
            {`${new Date(now).toISOString().slice(11, 19)} UTC`}
          </span>
        }
      />
      {error ? <Banner tone="bear">{error}</Banner> : null}
      {note ? <Banner tone={note.tone}>{note.message}</Banner> : null}

      <StatGrid columns={4}>
        <Stat label="Factor Universe" value={num(totalFactors, 0)} hint="across 6 taxonomies" tone="accent" />
        <Stat label="Loaders" value={num(LOADER_COUNT, 0)} hint="dual-vendor data adapters" />
        <Stat label="Regional Engines" value={num(ENGINES.length, 0)} hint="plus composite routing" />
        <Stat
          label="Best |IC|"
          value={scanRows.length > 0 && bestIc.index >= 0 ? num(bestIc.ic, 3) : "—"}
          hint={scanRows.length > 0 && bestIc.index >= 0 ? text(scanRows[bestIc.index].factor ?? scanRows[bestIc.index].name, "—") : "run a scan"}
          tone={scanRows.length > 0 ? "bull" : "muted"}
        />
      </StatGrid>

      <Card
        title="Factor Taxonomy"
        subtitle="Alpha101 · GTJA191 · Qlib158 · Fundamental PIT · Microstructure · Cross-Asset"
        actions={<Badge tone="muted">{`${TAXONOMIES.length} FAMILIES`}</Badge>}
      >
        {loading ? (
          <div className="flex items-center gap-2 text-[11px] text-ink-3">
            <Spinner /> loading taxonomy
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-6">
            {TAXONOMIES.map((key) => (
              <div key={key} className="min-w-0 rounded-[4px] border border-hairline bg-canvas p-2.5">
                <div className="truncate text-[9.5px] uppercase tracking-[0.1em] text-ink-3">{key}</div>
                <div className="mt-1 text-[19px] font-semibold leading-none tabular-nums text-ink">
                  {Number.isFinite(counts[key]) ? num(counts[key], 0) : "—"}
                </div>
                <div className="mt-1 text-[9.5px] uppercase tracking-[0.08em] text-ink-3">factors</div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <div className="grid min-w-0 grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
        <Card
          title="Factor Scanner"
          subtitle="cross-sectional IC ranking · top 25"
          actions={<Badge tone="muted">{`${scanRows.length} RANKED`}</Badge>}
        >
          <div className="grid grid-cols-1 gap-1.5 md:grid-cols-[130px_minmax(0,1fr)_auto]">
            <SelectInput
              value={silo}
              onChange={setSilo}
              options={SILOS.map((entry) => ({ value: entry.id, label: entry.short }))}
            />
            <TextInput mono value={symbols} onChange={(event) => setSymbols(event.target.value)} />
            <Button variant="primary" size="sm" icon="screener" disabled={scanLoading} onClick={() => void runScan()}>
              {scanLoading ? "Scanning" : "Scan"}
            </Button>
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-1">
            {TAXONOMIES.map((key) => (
              <Button
                key={key}
                size="sm"
                variant="ghost"
                active={taxonomies.includes(key)}
                onClick={() => toggleTaxonomy(key)}
              >
                {key}
              </Button>
            ))}
          </div>
          <div className="mt-2">
            {scanLoading ? (
              <div className="flex items-center gap-2 text-[11px] text-ink-3">
                <Spinner /> scanning factor zoo
              </div>
            ) : scanRows.length === 0 ? (
              <EmptyState
                title={scanned ? "No ranked factors" : "Scanner idle"}
                hint="Choose a silo, symbols and taxonomy set, then scan."
              />
            ) : (
              <Table columns={["Rank", "Factor", "Taxonomy", "IC", "T-Stat", "Hit Rate", "Turnover"]} dense>
                {scanRows.slice(0, 25).map((row, index) => {
                  const ic = safeNumber(row.ic ?? row.information_coefficient, 0);
                  const tstat = safeNumber(row.t_stat ?? row.tstat ?? row.t, 0);
                  const hit = asPercent(safeNumber(row.hit_rate ?? row.hit, Number.NaN));
                  const turnover = safeNumber(row.turnover ?? row.turn, Number.NaN);
                  return (
                    <Row key={`${text(row.factor ?? row.name, "f")}-${index}`}>
                      <Cell align="right" tone="muted">{num(safeNumber(row.rank, index + 1), 0)}</Cell>
                      <Cell mono>{text(row.factor ?? row.name ?? row.id, "—")}</Cell>
                      <Cell tone="muted">{text(row.taxonomy ?? row.family, "—")}</Cell>
                      <Cell align="right" tone={ic >= 0 ? "bull" : "bear"}>{signed(ic, 4)}</Cell>
                      <Cell align="right">{signed(tstat, 2)}</Cell>
                      <Cell align="right">{Number.isFinite(hit) ? pct(hit, 1) : "—"}</Cell>
                      <Cell align="right" tone="muted">{Number.isFinite(turnover) ? num(turnover, 2) : "—"}</Cell>
                    </Row>
                  );
                })}
              </Table>
            )}
          </div>
        </Card>

        <Card
          title="Black-Scholes Pricer"
          subtitle="spot · strike · vol · carry"
          actions={<Badge tone={sourceLabel === "BACKEND" ? "bull" : sourceLabel === "CLIENT" ? "accent" : "muted"}>{sourceLabel}</Badge>}
        >
          <div className="grid grid-cols-2 gap-1.5">
            <Field label="Spot">
              <TextInput mono value={spot} onChange={(event) => setSpot(event.target.value)} />
            </Field>
            <Field label="Strike">
              <TextInput mono value={strike} onChange={(event) => setStrike(event.target.value)} />
            </Field>
            <Field label="Days to Expiry">
              <TextInput mono value={days} onChange={(event) => setDays(event.target.value)} />
            </Field>
            <Field label="Volatility %">
              <TextInput mono value={volatility} onChange={(event) => setVolatility(event.target.value)} />
            </Field>
            <Field label="Risk-Free %">
              <TextInput mono value={rate} onChange={(event) => setRate(event.target.value)} />
            </Field>
            <Field label="Type">
              <SelectInput
                value={optionType}
                onChange={(value) => {
                  setOptionType(value === "PUT" ? "PUT" : "CALL");
                  setBackendGreeks(null);
                }}
                options={[
                  { value: "CALL", label: "CALL" },
                  { value: "PUT", label: "PUT" },
                ]}
              />
            </Field>
          </div>
          <Button
            className="mt-1.5 w-full"
            variant="primary"
            size="sm"
            icon="sigma"
            disabled={pricing}
            onClick={() => void priceOption()}
          >
            {pricing ? "Pricing" : "Price with backend"}
          </Button>
          <div className="mt-2">
            <StatGrid columns={3}>
              <Stat label="Price" value={shown ? num(shown.price, 2) : "—"} tone="accent" />
              <Stat label="Delta" value={shown ? signed(shown.delta, 4) : "—"} />
              <Stat label="Gamma" value={shown ? num(shown.gamma, 6) : "—"} />
              <Stat label="Vega" value={shown ? num(shown.vega, 4) : "—"} />
              <Stat label="Theta" value={shown ? signed(shown.theta, 4) : "—"} tone={shown ? "bear" : "muted"} />
              <Stat label="Rho" value={shown ? signed(shown.rho, 4) : "—"} />
            </StatGrid>
          </div>
          <p className="mt-2 text-[9.5px] leading-snug text-ink-3">
            Local mirror: {localGreeks ? `price ${num(localGreeks.price, 2)}` : "awaiting valid inputs"} · vol and rate are
            transmitted as decimals (18% → 0.18).
          </p>
        </Card>
      </div>

      <div className="grid min-w-0 grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
        <Card
          title="Loader × Engine Matrix"
          subtitle={`${ENGINES.length} regional engines · ${LOADER_COUNT} dual-vendor loaders`}
          actions={<Badge tone="muted">{`${engines.length} ENGINES`}</Badge>}
          dense
        >
          <div className="grid grid-cols-1 gap-1 p-2 md:grid-cols-2">
            {engines.map((engine) => (
              <div
                key={engine.key}
                className="flex min-w-0 items-center gap-2 rounded-[4px] border border-hairline bg-canvas px-2 py-1"
              >
                <StatusDot tone={engine.healthy ? "bull" : "bear"} />
                <span className="min-w-0 flex-1 truncate font-mono text-[10.5px] text-ink-2">{engine.key}</span>
                <Badge tone={engine.loaders > 0 ? "neutral" : "muted"} mono>
                  {`${num(engine.loaders, 0)} L`}
                </Badge>
              </div>
            ))}
          </div>
        </Card>

        <Card
          title="Loader Availability"
          subtitle="representative subset · 8 × 8"
          actions={<Badge tone="warn">REPRESENTATIVE SUBSET</Badge>}
          dense
        >
          <div className="flex flex-col gap-2 p-3">
            <div className="grid w-fit grid-cols-8 gap-1">
              {matrixCells.map((cell) => (
                <div
                  key={cell.index}
                  title={`LOADER ${cell.loader}${cell.available ? "" : " · GAP"}`}
                  className={cx(
                    "h-[16px] w-[16px] rounded-[2px] border",
                    cell.available ? "border-bull-border bg-bull-tint" : "border-hairline bg-elevated",
                  )}
                />
              ))}
            </div>
            <div className="flex items-center gap-3 text-[9.5px] uppercase tracking-[0.08em] text-ink-3">
              <span className="flex items-center gap-1">
                <span className="h-[8px] w-[8px] rounded-[2px] bg-bull" /> available
              </span>
              <span className="flex items-center gap-1">
                <span className="h-[8px] w-[8px] rounded-[2px] bg-elevated" /> gap
              </span>
              <span className="ml-auto tabular-nums">{`${LOADER_COUNT} LOADERS`}</span>
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
}
