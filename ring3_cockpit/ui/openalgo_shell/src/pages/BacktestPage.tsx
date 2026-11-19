import { useCallback, useEffect, useMemo, useState } from "react";
import { EquityDrawdownCurve } from "../charts";
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
  TextInput,
  Toolbar,
} from "../components/ui";
import { SILOS } from "../config/nav";
import { api, assertFinite, clampBars, errorMessage } from "../lib/api";
import { asArray, asRecord, int, num, pct, safeNumber, signed, text, titleize, upper } from "../lib/format";
import { rowsOf } from "../lib/payload";
import type { PageProps } from "./types";

type Note = { tone: "neutral" | "bull" | "bear"; message: string };

const TIMEFRAMES = ["1m", "5m", "15m", "1h", "1d"].map((value) => ({ value, label: value }));
const DATA_SOURCES = [
  { value: "SYNTHETIC_WALK_FORWARD", label: "Synthetic walk-forward" },
  { value: "TRADINGVIEW_REPLAY", label: "TradingView replay" },
];
const SILO_OPTIONS = SILOS.map((silo) => ({
  value: silo.id,
  label: `${silo.short} · ${silo.floorBps.toFixed(2)} bps`,
}));

const STRATEGY_FALLBACK = [
  "ma_golden_cross",
  "bullish_trend_alignment",
  "chanlun_pivot_breakout",
  "elliott_wave_impulse",
  "event_catalyst_momentum",
  "growth_quality_compounder",
  "expectation_rerating",
  "cross_asset_etf_rotation",
];

const DSR_GATE = 1.5;

function parseOptional(raw: string, label: string): number | null {
  if (raw.trim().length === 0) return null;
  return assertFinite(raw, label);
}

function deriveHorizon(series: number[], horizon: number): number {
  if (series.length <= horizon) return Number.NaN;
  let sum = 0;
  let count = 0;
  for (let index = 0; index + horizon < series.length; index += 1) {
    const start = series[index];
    const end = series[index + horizon];
    if (start > 0 && Number.isFinite(end)) {
      sum += (end / start - 1) * 10_000;
      count += 1;
    }
  }
  return count > 0 ? sum / count : Number.NaN;
}

export function BacktestPage({ mode }: PageProps) {
  const [catalog, setCatalog] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<Note | null>(null);
  const [busy, setBusy] = useState(false);

  const [silo, setSilo] = useState("US_IBKR");
  const [symbol, setSymbol] = useState("SPY");
  const [strategy, setStrategy] = useState("ma_golden_cross");
  const [timeframe, setTimeframe] = useState("1d");
  const [barsInput, setBarsInput] = useState("252");
  const [capitalInput, setCapitalInput] = useState("100000");
  const [dataSource, setDataSource] = useState("SYNTHETIC_WALK_FORWARD");
  const [replayInput, setReplayInput] = useState("0");
  const [fastInput, setFastInput] = useState("");
  const [slowInput, setSlowInput] = useState("");
  const [stopInput, setStopInput] = useState("");
  const [targetInput, setTargetInput] = useState("");

  const [run, setRun] = useState<Record<string, unknown> | null>(null);
  const [lastRun, setLastRun] = useState<Record<string, unknown> | null>(null);
  const [compareRows, setCompareRows] = useState<Record<string, unknown>[]>([]);
  const [promotion, setPromotion] = useState<Record<string, unknown> | null>(null);

  const loadCatalog = useCallback(async () => {
    try {
      const payload = await api.get<unknown>("/cockpit/backtest");
      setCatalog(asRecord(payload));
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadCatalog();
  }, [loadCatalog]);

  const strategies = useMemo(() => {
    const rows = rowsOf(catalog, ["strategies"]);
    const source =
      rows.length > 0 ? rows.map((row) => text(row.strategy_id, "")).filter(Boolean) : STRATEGY_FALLBACK;
    return source.map((value) => ({ value, label: titleize(value) }));
  }, [catalog]);

  const selectedSilo = SILOS.find((entry) => entry.id === silo) ?? SILOS[0];
  const metrics = useMemo(() => asRecord(run?.metrics), [run]);
  const equity = useMemo(() => asArray(run?.equity_curve).map((value) => safeNumber(value, 0)), [run]);
  const drawdown = useMemo(
    () => asArray(run?.drawdown_curve_pct).map((value) => safeNumber(value, 0)),
    [run],
  );

  const netBps = safeNumber(metrics.net_edge_bps ?? run?.net_edge_bps, Number.NaN);
  const grossBps = safeNumber(metrics.gross_edge_bps, Number.NaN);
  const taxBps = safeNumber(metrics.tax_and_cost_drag_bps ?? run?.cost_floor_bps, Number.NaN);
  const sharpe = safeNumber(metrics.sharpe_ratio, Number.NaN);
  const dsr = safeNumber(run?.deflated_sharpe_ratio, Number.NaN);
  const maxDrawdownPct = safeNumber(metrics.max_drawdown_pct, Number.NaN);
  const tradeCount = safeNumber(metrics.total_trades ?? run?.trade_count, Number.NaN);
  const winRate = safeNumber(metrics.win_rate_pct, Number.NaN);
  const dsrPass = Number.isFinite(dsr) ? dsr >= DSR_GATE : false;
  const netPass = Number.isFinite(netBps) ? netBps > 0 : false;
  const backtestId = text(run?.backtest_id, "");
  const promotable = dsrPass && netPass && backtestId.length > 0;

  const horizons = useMemo(() => {
    const matrix = asRecord(run?.horizon_matrix);
    return [
      { label: "T+1", key: "t_plus_1", bars: 1 },
      { label: "T+5", key: "t_plus_5", bars: 5 },
      { label: "T+20", key: "t_plus_20", bars: 20 },
    ].map((entry) => {
      const row = asRecord(matrix[entry.key]);
      const measured = safeNumber(row.net_edge_bps, Number.NaN);
      const derived = Number.isFinite(measured) ? measured : deriveHorizon(equity, entry.bars);
      return {
        label: entry.label,
        net: derived,
        derived: !Number.isFinite(measured),
        win: safeNumber(row.win_rate_pct, Number.NaN),
        samples: safeNumber(row.samples, Number.NaN),
      };
    });
  }, [run, equity]);

  const execute = useCallback(async () => {
    setBusy(true);
    setNote(null);
    setPromotion(null);
    try {
      const capital = assertFinite(capitalInput, "initial capital");
      if (capital <= 0) throw new Error("initial capital must be greater than zero");
      const fast = parseOptional(fastInput, "fast window");
      const slow = parseOptional(slowInput, "slow window");
      if (fast !== null && slow !== null && fast >= slow) {
        throw new Error("fast window must be shorter than the slow window");
      }
      const offset = Math.max(
        0,
        Math.min(5000, Math.trunc(assertFinite(replayInput.trim() || "0", "replay offset"))),
      );
      const body = {
        silo,
        symbol: upper(symbol.trim()),
        strategy_id: strategy,
        timeframe,
        bars: clampBars(barsInput, 252),
        initial_capital_units: capital,
        fast_window: fast,
        slow_window: slow,
        stop_loss_bps: parseOptional(stopInput, "stop loss bps"),
        take_profit_bps: parseOptional(targetInput, "take profit bps"),
        data_source: dataSource,
        replay_offset: offset,
      };
      if (body.symbol.length === 0) throw new Error("a symbol is required");
      const payload = await api.post<unknown>("/control/backtest/run", body);
      const receipt = asRecord(payload);
      setRun(receipt);
      setLastRun({
        backtest_id: receipt.backtest_id,
        strategy_id: receipt.strategy_id,
        symbol: receipt.symbol,
        silo: receipt.silo,
        timeframe: receipt.timeframe,
        net_edge_bps: asRecord(receipt.metrics).net_edge_bps,
        deflated_sharpe_ratio: receipt.deflated_sharpe_ratio,
        total_return_pct: asRecord(receipt.metrics).total_return_pct,
        verified: receipt.verified,
      });
      setNote({
        tone: receipt.verified === true ? "bull" : "neutral",
        message: `${text(receipt.backtest_id, "RUN")} · ${upper(receipt.strategy_id)} · NET ${num(asRecord(receipt.metrics).net_edge_bps, 2)} bps · DSR ${num(receipt.deflated_sharpe_ratio, 2)}`,
      });
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }, [
    barsInput,
    capitalInput,
    dataSource,
    fastInput,
    replayInput,
    silo,
    slowInput,
    stopInput,
    strategy,
    symbol,
    targetInput,
    timeframe,
  ]);

  const compare = useCallback(async () => {
    setBusy(true);
    setNote(null);
    try {
      const body = {
        silo,
        symbol: upper(symbol.trim()),
        timeframe,
        bars: clampBars(barsInput, 252),
      };
      if (body.symbol.length === 0) throw new Error("a symbol is required");
      const payload = await api.post<unknown>("/control/backtest/compare-strategies", body);
      const rows = rowsOf(payload, ["leaderboard", "ranked"]).sort(
        (left, right) => safeNumber(right.net_edge_bps, 0) - safeNumber(left.net_edge_bps, 0),
      );
      setCompareRows(rows);
      setNote({
        tone: rows.length > 0 ? "neutral" : "bear",
        message:
          rows.length > 0
            ? `${rows.length} STRATEGIES COMPARED · SORTED BY NET bps`
            : "the comparison returned no rows",
      });
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
      setCompareRows([]);
    } finally {
      setBusy(false);
    }
  }, [barsInput, silo, symbol, timeframe]);

  const promote = useCallback(async () => {
    if (!promotable) {
      setNote({
        tone: "bear",
        message:
          "Promotion is blocked: the run must clear the DSR >= 1.50 gate with positive net expectancy.",
      });
      return;
    }
    setBusy(true);
    try {
      const payload = await api.post<unknown>("/control/backtest/promote-to-nursery", {
        backtest_id: backtestId,
      });
      const record = asRecord(payload);
      setPromotion(record);
      setNote({
        tone: record.promoted === true ? "bull" : "neutral",
        message: `${text(record.plugin_name, "PLUGIN")} · ${record.promoted === true ? "IN NURSERY" : "NOT PROMOTED"} · ${int(record.remaining_fills)} shadow fills remaining`,
      });
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }, [backtestId, promotable]);

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="BACKTEST"
        subtitle="Tax-net walk-forward, purged K-fold, Deflated Sharpe gate and the 30-fill nursery"
        badges={
          <>
            <Badge tone={mode === "LIVE" ? "bear" : "accent"} mono>
              {mode}
            </Badge>
            <Badge tone="muted" mono>
              DSR GATE ≥ {DSR_GATE.toFixed(2)}
            </Badge>
            <Badge tone="neutral" mono>
              {selectedSilo.short} FLOOR {selectedSilo.floorBps.toFixed(2)} bps
            </Badge>
          </>
        }
        actions={
          <Button icon="refresh" variant="subtle" size="sm" onClick={() => void loadCatalog()}>
            Catalog
          </Button>
        }
      />

      {error ? <Banner tone="bear">{error}</Banner> : null}
      {note ? <Banner tone={note.tone}>{note.message}</Banner> : null}

      <StatGrid columns={4}>
        <Stat
          label="Net expectancy"
          value={Number.isFinite(netBps) ? `${signed(netBps, 2)} bps` : "—"}
          hint="after statutory tax + spread"
          tone={Number.isFinite(netBps) ? (netBps > 0 ? "bull" : "bear") : "muted"}
        />
        <Stat
          label="Deflated Sharpe"
          value={Number.isFinite(dsr) ? num(dsr, 2) : "—"}
          hint={`gate ${DSR_GATE.toFixed(2)}`}
          tone={Number.isFinite(dsr) ? (dsrPass ? "bull" : "bear") : "muted"}
        />
        <Stat
          label="Max drawdown"
          value={Number.isFinite(maxDrawdownPct) ? pct(maxDrawdownPct, 2) : "—"}
          tone="neutral"
        />
        <Stat
          label="Trades"
          value={Number.isFinite(tradeCount) ? int(tradeCount) : "—"}
          hint={Number.isFinite(winRate) ? `${num(winRate, 1)}% win rate` : undefined}
        />
      </StatGrid>

      <Card title="Walk-forward configuration" subtitle="POST /control/backtest/run" icon="backtest">
        <div className="grid gap-2 md:grid-cols-3 xl:grid-cols-6">
          <Field label="Silo">
            <SelectInput value={silo} onChange={setSilo} options={SILO_OPTIONS} />
          </Field>
          <Field label="Symbol">
            <TextInput
              value={symbol}
              mono
              onChange={(event) => setSymbol(event.target.value.toUpperCase())}
            />
          </Field>
          <Field label="Strategy">
            <SelectInput value={strategy} onChange={setStrategy} options={strategies} />
          </Field>
          <Field label="Timeframe">
            <SelectInput value={timeframe} onChange={setTimeframe} options={TIMEFRAMES} />
          </Field>
          <Field label="Bars" hint={`clamped 1…${5000}`}>
            <TextInput value={barsInput} mono onChange={(event) => setBarsInput(event.target.value)} />
          </Field>
          <Field label="Initial capital">
            <TextInput value={capitalInput} mono onChange={(event) => setCapitalInput(event.target.value)} />
          </Field>
          <Field label="Data source">
            <SelectInput value={dataSource} onChange={setDataSource} options={DATA_SOURCES} />
          </Field>
          <Field label="Replay offset" hint="0…5000 · TradingView only">
            <TextInput
              value={replayInput}
              mono
              disabled={dataSource !== "TRADINGVIEW_REPLAY"}
              onChange={(event) => setReplayInput(event.target.value)}
            />
          </Field>
          <Field label="Fast window" hint="blank = strategy default">
            <TextInput value={fastInput} mono onChange={(event) => setFastInput(event.target.value)} />
          </Field>
          <Field label="Slow window">
            <TextInput value={slowInput} mono onChange={(event) => setSlowInput(event.target.value)} />
          </Field>
          <Field label="Stop loss bps">
            <TextInput value={stopInput} mono onChange={(event) => setStopInput(event.target.value)} />
          </Field>
          <Field label="Take profit bps">
            <TextInput value={targetInput} mono onChange={(event) => setTargetInput(event.target.value)} />
          </Field>
        </div>
        <Toolbar className="mt-2">
          <Button variant="primary" size="sm" icon="play" disabled={busy} onClick={() => void execute()}>
            Run walk-forward
          </Button>
          <Button variant="subtle" size="sm" icon="screener" disabled={busy} onClick={() => void compare()}>
            Compare 8 strategies
          </Button>
          <Button
            variant="success"
            size="sm"
            icon="target"
            disabled={busy || !promotable}
            onClick={() => void promote()}
            title={
              promotable ? "Register into the nursery" : "Blocked until DSR >= 1.50 and net expectancy > 0"
            }
          >
            Promote to nursery
          </Button>
          {busy ? <Spinner /> : null}
          {!promotable ? (
            <span className="text-[10px] uppercase tracking-[0.1em] text-ink-3">
              PROMOTION BLOCKED · DSR {Number.isFinite(dsr) ? num(dsr, 2) : "—"} / NET{" "}
              {Number.isFinite(netBps) ? num(netBps, 2) : "—"} bps
            </span>
          ) : null}
        </Toolbar>
        <p className="mt-2 text-[10px] uppercase tracking-[0.1em] text-ink-3">
          COST FLOORS ARE STATUTORY ·{" "}
          {SILOS.map((entry) => `${entry.short} ${entry.floorBps.toFixed(2)} bps`).join(" · ")}
        </p>
      </Card>

      <div className="grid gap-3 xl:grid-cols-[1.35fr_1fr]">
        <Card
          title="Run result"
          subtitle={backtestId || "no run yet"}
          icon="activity"
          actions={
            <Badge tone={Number.isFinite(dsr) ? (dsrPass ? "bull" : "bear") : "muted"} mono>
              {Number.isFinite(dsr) ? (dsrPass ? "DSR PASS" : "DSR FAIL") : "DSR —"}
            </Badge>
          }
        >
          {loading ? (
            <div className="flex items-center gap-2 text-[11px] text-ink-3">
              <Spinner /> loading catalog…
            </div>
          ) : run === null ? (
            <EmptyState
              title="No completed run"
              hint="Configure the walk-forward and run it to produce a tax-net equity curve."
            />
          ) : (
            <div className="flex flex-col gap-2">
              <KeyValue
                columns={3}
                items={[
                  {
                    label: "Net bps",
                    value: Number.isFinite(netBps) ? signed(netBps, 2) : "—",
                    tone: netPass ? "bull" : "bear",
                  },
                  { label: "Gross bps", value: Number.isFinite(grossBps) ? signed(grossBps, 2) : "—" },
                  { label: "Tax drag bps", value: Number.isFinite(taxBps) ? num(taxBps, 2) : "—" },
                  { label: "Sharpe", value: Number.isFinite(sharpe) ? num(sharpe, 2) : "—" },
                  {
                    label: "Deflated Sharpe",
                    value: Number.isFinite(dsr) ? num(dsr, 3) : "—",
                    tone: dsrPass ? "bull" : "bear",
                  },
                  { label: "Trades", value: Number.isFinite(tradeCount) ? int(tradeCount) : "—" },
                  { label: "Win rate", value: Number.isFinite(winRate) ? pct(winRate, 1) : "—" },
                  { label: "Max DD", value: Number.isFinite(maxDrawdownPct) ? pct(maxDrawdownPct, 2) : "—" },
                  {
                    label: "Leakage free",
                    value: text(run.leakage_free ?? asRecord(run.purged_kfold).leakage_free, "—"),
                  },
                ]}
              />
              <EquityDrawdownCurve equity={equity} drawdown={drawdown} height={210} />
              <div className="flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-[0.1em] text-ink-3">
                <span>{text(run.strategy_name ?? run.strategy_id, "—")}</span>
                <span>·</span>
                <span>{text(run.timeframe, "—")}</span>
                <span>·</span>
                <span>{int(run.bars)} bars</span>
                <span>·</span>
                <span>{text(run.data_source, "—")}</span>
                <span>·</span>
                <span>GROUNDING {text(run.grounding_sha256, "").slice(0, 12) || "—"}</span>
              </div>
            </div>
          )}
        </Card>

        <Card title="Horizon matrix" subtitle="T+1 / T+5 / T+20 net edge per horizon" icon="clock">
          <div className="grid grid-cols-3 gap-2">
            {horizons.map((horizon) => (
              <div
                key={horizon.label}
                className="flex flex-col gap-1 rounded-[4px] border border-hairline bg-canvas px-2 py-2"
              >
                <span className="text-[9.5px] uppercase tracking-[0.1em] text-ink-3">{horizon.label}</span>
                <span
                  className={`text-[15px] font-semibold tabular-nums ${Number.isFinite(horizon.net) ? (horizon.net > 0 ? "text-bull" : "text-bear") : "text-ink-3"}`}
                >
                  {Number.isFinite(horizon.net) ? signed(horizon.net, 2) : "—"}
                </span>
                <span className="text-[9.5px] uppercase tracking-[0.08em] text-ink-3">
                  {horizon.derived ? "derived · equity" : "measured"}
                </span>
                <span className="text-[10px] tabular-nums text-ink-3">
                  {Number.isFinite(horizon.win) ? `${num(horizon.win, 1)}% wins` : "— wins"}
                </span>
                <span className="text-[10px] tabular-nums text-ink-3">
                  {Number.isFinite(horizon.samples) ? `${int(horizon.samples)} samples` : "— samples"}
                </span>
              </div>
            ))}
          </div>
          {lastRun === null ? (
            <p className="mt-2 text-[10px] uppercase tracking-[0.1em] text-ink-3">
              LAST RUN SUMMARY PERSISTS IN SESSION STATE ONCE A RUN COMPLETES
            </p>
          ) : (
            <div className="mt-2 border-t border-hairline pt-2 text-[10px] uppercase tracking-[0.08em] text-ink-3">
              LAST RUN · {text(lastRun.backtest_id, "—")} · {upper(lastRun.strategy_id)} · NET{" "}
              {num(lastRun.net_edge_bps, 2)} bps · DSR {num(lastRun.deflated_sharpe_ratio, 2)} · RETURN{" "}
              {num(lastRun.total_return_pct, 2)}%
            </div>
          )}
          {promotion !== null ? (
            <div className="mt-2 rounded-[4px] border border-bull-border bg-bull-tint px-2 py-1.5 text-[10px] uppercase tracking-[0.08em] text-bull">
              NURSERY · {text(promotion.plugin_name, "—")} · {int(promotion.shadow_fills)}/
              {int(promotion.required_fills)} fills · graduated {text(promotion.graduated, "—")}
            </div>
          ) : null}
        </Card>
      </div>

      <Card
        title="8-strategy comparison"
        subtitle="POST /control/backtest/compare-strategies · sorted by net bps"
        icon="layer"
        dense
      >
        {compareRows.length === 0 ? (
          <div className="p-3">
            <EmptyState
              title="No comparison yet"
              hint="Run the comparison to score all eight harvested templates on one identical bar series."
            />
          </div>
        ) : (
          <Table columns={["STRATEGY", "NET bps", "GROSS bps", "TAX bps", "DSR", "TRADES", "VERDICT"]}>
            {compareRows.map((row) => {
              const rowNet = safeNumber(row.net_edge_bps, Number.NaN);
              const floor = safeNumber(row.cost_floor_bps, selectedSilo.floorBps);
              const rowGross = safeNumber(
                row.gross_edge_bps,
                Number.isFinite(rowNet) ? rowNet + floor : Number.NaN,
              );
              const rowDsr = safeNumber(row.deflated_sharpe_ratio, Number.NaN);
              const rowTax = safeNumber(row.tax_and_cost_drag_bps ?? row.cost_floor_bps, Number.NaN);
              const pass = row.passes_dsr_gate === true && Number.isFinite(rowNet) && rowNet > 0;
              return (
                <Row key={text(row.strategy_id)}>
                  <Cell>
                    <div className="flex min-w-0 flex-col">
                      <span className="truncate text-ink-2">
                        {text(row.strategy_name ?? titleize(row.strategy_id))}
                      </span>
                      <span className="truncate font-mono text-[10px] text-ink-3">
                        {text(row.strategy_id)}
                      </span>
                    </div>
                  </Cell>
                  <Cell
                    align="right"
                    mono
                    tone={Number.isFinite(rowNet) ? (rowNet > 0 ? "bull" : "bear") : "muted"}
                  >
                    {Number.isFinite(rowNet) ? signed(rowNet, 2) : "—"}
                  </Cell>
                  <Cell align="right" mono>
                    {Number.isFinite(rowGross) ? signed(rowGross, 2) : "—"}
                  </Cell>
                  <Cell align="right" mono tone="muted">
                    {Number.isFinite(rowTax) ? num(rowTax, 2) : "—"}
                  </Cell>
                  <Cell
                    align="right"
                    mono
                    tone={Number.isFinite(rowDsr) ? (rowDsr >= DSR_GATE ? "bull" : "bear") : "muted"}
                  >
                    {Number.isFinite(rowDsr) ? num(rowDsr, 2) : "—"}
                  </Cell>
                  <Cell align="right" mono>
                    {int(row.total_trades)}
                  </Cell>
                  <Cell align="right">
                    <Badge tone={pass ? "bull" : "bear"} mono>
                      {pass ? "PASS" : "REJECT"}
                    </Badge>
                  </Cell>
                </Row>
              );
            })}
          </Table>
        )}
        <p className="px-3 py-2 text-[10px] uppercase tracking-[0.1em] text-ink-3">
          GROSS IS RECONSTRUCTED AS NET + STATUTORY FLOOR WHERE THE COMPARISON OMITS IT · {selectedSilo.short}{" "}
          {selectedSilo.floorBps.toFixed(2)} bps
        </p>
      </Card>
    </div>
  );
}

export default BacktestPage;
