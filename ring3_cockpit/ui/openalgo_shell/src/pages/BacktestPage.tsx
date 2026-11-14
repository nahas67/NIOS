/**
 * Ring 2 walk-forward backtester.
 *
 * Every run replays a deterministic synthetic tape, charges the selected silo's
 * cost floor and statutory tax on each trade, then certifies the result with a
 * Deflated Sharpe gate and a purged K-fold audit. A run reaches the 30-fill
 * shadow nursery only when both gates clear; nothing here can sign an order.
 */

import { useState } from "react";
import type { ReactNode } from "react";
import { api } from "@/lib/api";
import type {
  BacktestComparisonReceipt,
  BacktestReceipt,
  BacktestTimeframe,
  NurseryPromotionReceipt,
} from "@/lib/types";
import { ago, int, num, record, shortId, text, utc } from "@/lib/format";
import { useAction, useResource } from "@/lib/use-api";
import { navigate, pagePath, useLocation } from "@/lib/router";
import {
  CopyHash,
  EmptyCleanSlate,
  ErrorBlock,
  GlassPanel,
  InlineBar,
  LoadingBlock,
  MetricTile,
  SectionHeader,
  StatusPill,
  TabStrip,
  TabularValue,
  passTone,
  type PillTone,
} from "@/design-system/primitives";
import { EquityDrawdownChart } from "@/charts/EquityDrawdownChart";

const DEFAULT_SYMBOL = "SPY";
const DEFAULT_BARS = "252";
const DEFAULT_CAPITAL = "100000";

function readPositive(value: string): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function sideTone(side: string): PillTone {
  const value = side.toUpperCase();
  if (value === "BUY" || value === "LONG") return "ok";
  if (value === "SELL" || value === "SHORT") return "bad";
  return "neutral";
}

function InlineAlert({ message }: { message: string }): ReactNode {
  return (
    <p
      role="alert"
      className="rounded-[var(--r-sm)] border border-[rgba(251,191,36,0.35)] bg-[var(--warn-soft)] px-2.5 py-1.5 text-[11px] text-[var(--warn)]"
    >
      {message}
    </p>
  );
}

export function BacktestPage(): ReactNode {
  const location = useLocation();
  const workspace = location.workspace;
  const res = useResource("backtest", () => api.backtest(), 20_000);
  const runAction = useAction<BacktestReceipt>();
  const compareAction = useAction<BacktestComparisonReceipt>();
  const promoteAction = useAction<NurseryPromotionReceipt>();

  const [siloChoice, setSiloChoice] = useState("");
  const [strategyChoice, setStrategyChoice] = useState("");
  const [timeframeChoice, setTimeframeChoice] = useState<BacktestTimeframe | "">("");
  const [symbolInput, setSymbolInput] = useState(DEFAULT_SYMBOL);
  const [barsInput, setBarsInput] = useState(DEFAULT_BARS);
  const [capitalInput, setCapitalInput] = useState(DEFAULT_CAPITAL);
  const [formError, setFormError] = useState<string | null>(null);

  const data = res.data;
  const silos = data?.silos ?? [];
  const strategies = data?.strategies ?? [];
  const timeframes = data?.timeframes ?? [];
  const floorRows = data?.cost_floor_table ?? [];

  const silo = siloChoice || silos[0] || "US_IBKR";
  const strategyId = strategyChoice || strategies[0]?.strategy_id || "";
  const timeframe: BacktestTimeframe = timeframeChoice || data?.default_timeframe || "1d";
  const selectedFloor = floorRows.find((row) => row.silo === silo);
  const floorBps = selectedFloor?.cost_floor_bps ?? data?.cost_floors?.[silo] ?? 0;
  const taxBps = selectedFloor?.statutory_tax_bps ?? 0;

  const receipt = runAction.receipt;
  const metrics = record(receipt?.metrics);
  const purged = receipt?.purged_kfold;
  const totalReturnPct = receipt
    ? (metrics.total_return_pct ??
      (receipt.initial_capital_units > 0
        ? (receipt.final_nav_units / receipt.initial_capital_units - 1) * 100
        : null))
    : null;
  const dsrGate = receipt?.dsr_gate ?? data?.dsr_gate ?? 1.5;
  const netFloor = receipt?.champion_net_edge_floor_bps ?? data?.champion_net_edge_floor_bps ?? 0.5;

  const horizonRows = receipt
    ? Object.entries(receipt.horizon_matrix)
        .map(([key, row]) => ({ key, row }))
        .sort((left, right) => left.row.horizon_bars - right.row.horizon_bars)
    : [];
  const trades = receipt?.trades ?? [];

  const comparison = compareAction.receipt;
  const leaderboard =
    comparison && Array.isArray(comparison.leaderboard) ? comparison.leaderboard : [];
  const runs = data
    ? [...data.runs].sort((left, right) => right.created_at_ns - left.created_at_ns).slice(0, 12)
    : [];

  const submitRun = async (): Promise<void> => {
    setFormError(null);
    const symbol = symbolInput.trim().toUpperCase();
    const bars = readPositive(barsInput);
    const capital = readPositive(capitalInput);
    if (!symbol) {
      setFormError("Enter a symbol before running the tape.");
      return;
    }
    if (!strategyId) {
      setFormError("Choose a strategy template.");
      return;
    }
    if (bars === null || !Number.isInteger(bars)) {
      setFormError("Bars must be a positive whole number.");
      return;
    }
    if (capital === null) {
      setFormError("Initial capital must be positive.");
      return;
    }
    const result = await runAction.run(() =>
      api.runBacktest({
        silo,
        symbol,
        strategy_id: strategyId,
        bars,
        timeframe,
        initial_capital_units: capital,
      }),
    );
    if (result) {
      promoteAction.reset();
      res.reload();
    }
  };

  const submitCompare = async (): Promise<void> => {
    setFormError(null);
    const symbol = symbolInput.trim().toUpperCase();
    const bars = readPositive(barsInput);
    if (!symbol) {
      setFormError("Enter a symbol before comparing strategies.");
      return;
    }
    if (bars === null || !Number.isInteger(bars)) {
      setFormError("Bars must be a positive whole number.");
      return;
    }
    const result = await compareAction.run(() =>
      api.compareStrategies({ silo, symbol, timeframe, bars }),
    );
    if (result) res.reload();
  };

  const promote = async (): Promise<void> => {
    if (!receipt) return;
    const result = await promoteAction.run(() => api.promoteToNursery(receipt.backtest_id));
    if (result) res.reload();
  };

  return (
    <div className="flex flex-col gap-[14px]">
      <SectionHeader
        eyebrow="Ring 2 · backtester"
        title="Walk-Forward Backtester"
        subtitle="Tax-net walk-forward runs with Deflated Sharpe, purged K-fold certification and horizon accuracy. A synthetic tape only — no broker key ever enters this ring."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill
              value={`${int(data?.run_count ?? 0)} saved runs`}
              tone={(data?.run_count ?? 0) > 0 ? "accent" : "neutral"}
            />
            <StatusPill value={`DSR gate ${num(data?.dsr_gate ?? 1.5, 2)}`} tone="accent2" />
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              onClick={res.reload}
              aria-label="Reload backtester telemetry"
            >
              Refresh
            </button>
          </div>
        }
      />

      {!data && res.loading ? <LoadingBlock rows={4} /> : null}
      {!data && res.error ? <ErrorBlock message={res.error} onRetry={res.reload} /> : null}
      {data && res.error ? <ErrorBlock message={res.error} onRetry={res.reload} /> : null}

      {data ? (
        <>
          <GlassPanel
            eyebrow="Run parameters"
            title="Tax-net walk-forward"
            actions={
              <StatusPill
                value={`${int(data.strategy_count)} strategies · ${int(data.k_folds)} purged folds`}
                tone="accent"
              />
            }
          >
            <div className="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-4">
              <label className="field">
                <span className="field-label">Silo</span>
                <select
                  className="select"
                  value={silo}
                  onChange={(event) => setSiloChoice(event.target.value)}
                  aria-label="Backtest silo"
                >
                  {silos.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
                <span
                  className="mono tnum text-[9.5px] text-[var(--dim)]"
                  title="Cost floor plus statutory tax charged on every trade in this silo"
                >
                  {num(floorBps, 2)} bps floor · {num(taxBps, 2)} bps statutory tax
                </span>
              </label>

              <label className="field">
                <span className="field-label">Symbol</span>
                <input
                  className="input mono"
                  value={symbolInput}
                  onChange={(event) => setSymbolInput(event.target.value)}
                  autoComplete="off"
                  aria-label="Backtest symbol"
                />
              </label>

              <label className="field">
                <span className="field-label">Strategy</span>
                <select
                  className="select"
                  value={strategyId}
                  onChange={(event) => setStrategyChoice(event.target.value)}
                  aria-label="Backtest strategy"
                >
                  {strategies.map((strategy) => (
                    <option key={strategy.strategy_id} value={strategy.strategy_id}>
                      {strategy.name} · {strategy.strategy_id}
                    </option>
                  ))}
                </select>
              </label>

              <label className="field">
                <span className="field-label">Bars</span>
                <input
                  className="input tnum"
                  inputMode="numeric"
                  value={barsInput}
                  onChange={(event) => setBarsInput(event.target.value)}
                  aria-label="Backtest bar count"
                />
                <span className="mono tnum text-[9.5px] text-[var(--dim)]">60–1000 bars replay the tape</span>
              </label>

              <div className="field sm:col-span-2">
                <span className="field-label">Timeframe</span>
                <div>
                  <TabStrip
                    tabs={timeframes.map((item) => ({ id: item, label: item }))}
                    active={timeframe}
                    onChange={(id) => setTimeframeChoice(id)}
                    label="Backtest timeframe"
                  />
                </div>
              </div>

              <label className="field">
                <span className="field-label">Initial capital (units)</span>
                <input
                  className="input tnum"
                  inputMode="decimal"
                  value={capitalInput}
                  onChange={(event) => setCapitalInput(event.target.value)}
                  aria-label="Initial capital in account units"
                />
              </label>
            </div>

            <div className="mt-2.5 flex flex-wrap items-center gap-2">
              <button
                type="button"
                className="btn btn-accent"
                onClick={() => void submitRun()}
                disabled={runAction.busy}
                aria-label="Run a walk-forward backtest"
              >
                {runAction.busy ? "Replaying tape…" : "Run Backtest"}
              </button>
              <button
                type="button"
                className="btn"
                onClick={() => void submitCompare()}
                disabled={compareAction.busy}
                aria-label="Compare all eight strategies on this tape"
              >
                {compareAction.busy ? "Comparing…" : "Compare All 8 Strategies"}
              </button>
              <span className="mono tnum text-[9.5px] text-[var(--dim)]">
                gate {num(data.dsr_gate, 2)} DSR · floor +{num(data.champion_net_edge_floor_bps, 2)} bps ·{" "}
                {num(data.embargo_pct * 100, 2)}% embargo
              </span>
            </div>

            {formError ? <div className="mt-2"><InlineAlert message={formError} /></div> : null}
            {runAction.error ? <div className="mt-2"><ErrorBlock message={runAction.error} /></div> : null}
          </GlassPanel>

          {receipt ? (
            <>
              <GlassPanel
                eyebrow={`${receipt.silo} · ${receipt.timeframe}`}
                title={`${receipt.symbol} · ${receipt.strategy_name}`}
                actions={
                  <div className="flex flex-wrap items-center gap-1.5">
                    <CopyHash value={receipt.grounding_sha256} label="copy backtest grounding digest" />
                    <StatusPill
                      value={receipt.verified ? "VERIFIED" : "UNVERIFIED"}
                      tone={passTone(receipt.verified)}
                    />
                  </div>
                }
              >
                <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
                  <MetricTile
                    label="Initial capital"
                    value={<TabularValue value={receipt.initial_capital_units} digits={0} />}
                    hint="account units"
                  />
                  <MetricTile
                    label="Final NAV"
                    value={<TabularValue value={receipt.final_nav_units} digits={0} />}
                    hint="after tax-net costs"
                  />
                  <MetricTile
                    label="Total return"
                    value={<TabularValue value={totalReturnPct} kind="pct" signTone />}
                    hint={`${int(receipt.trade_count)} trades`}
                  />
                  <MetricTile
                    label="Max drawdown"
                    value={<TabularValue value={metrics.max_drawdown_pct} kind="pct" />}
                    hint="deepest underwater trough"
                  />
                  <MetricTile
                    label="Win rate"
                    value={<TabularValue value={metrics.win_rate_pct} kind="pct" />}
                    hint="closed trades"
                  />
                </div>
                <div className="mt-3">
                  <EquityDrawdownChart
                    equity={receipt.equity_curve}
                    benchmark={receipt.benchmark_curve_units}
                    drawdownPct={receipt.drawdown_curve_pct}
                  />
                </div>
              </GlassPanel>

              <GlassPanel
                eyebrow="Certification"
                title="Walk-forward gates"
                actions={
                  <StatusPill
                    value={receipt.promotable ? "CERTIFIED" : "BLOCKED"}
                    tone={receipt.promotable ? "ok" : "bad"}
                  />
                }
              >
                <div
                  className={`flex flex-wrap items-center gap-2 rounded-[var(--r-md)] border p-3 ${
                    receipt.promotable
                      ? "border-[rgba(52,211,153,0.35)] bg-[var(--pos-soft)]"
                      : "border-[rgba(251,113,133,0.35)] bg-[var(--neg-soft)]"
                  }`}
                >
                  <StatusPill
                    value={receipt.promotable ? "CERTIFIED FOR NURSERY" : "NOT CERTIFIED"}
                    tone={receipt.promotable ? "ok" : "bad"}
                  />
                  <span className="mono tnum text-[11px] text-[var(--ink-soft)]">
                    DSR {num(receipt.deflated_sharpe_ratio, 2)} vs gate {num(dsrGate, 2)}
                  </span>
                  <span className="text-[var(--faint)]">·</span>
                  <span className="mono tnum text-[11px] text-[var(--ink-soft)]">
                    net {num(receipt.net_edge_bps, 2)} bps vs floor +{num(netFloor, 2)} bps
                  </span>
                  <StatusPill
                    value={receipt.passes_dsr_gate ? "DSR PASS" : "DSR FAIL"}
                    tone={passTone(receipt.passes_dsr_gate)}
                  />
                  <StatusPill
                    value={receipt.passes_champion_floor ? "EDGE PASS" : "EDGE FAIL"}
                    tone={passTone(receipt.passes_champion_floor)}
                  />
                  <button
                    type="button"
                    className="btn btn-sm btn-accent ml-auto"
                    onClick={() => void promote()}
                    disabled={
                      !receipt.promotable ||
                      promoteAction.busy ||
                      promoteAction.receipt?.promoted === true
                    }
                    aria-label="Promote this backtest to the 30-fill shadow nursery"
                  >
                    {promoteAction.busy ? "Promoting…" : "Promote to 30-Fill Nursery"}
                  </button>
                </div>

                {!receipt.promotable ? (
                  <p className="mono mt-1.5 text-[9.5px] text-[var(--dim)]">
                    both the Deflated Sharpe gate and the champion net-edge floor must clear before the nursery
                    accepts this run
                  </p>
                ) : null}

                {promoteAction.error ? (
                  <div className="mt-2">
                    <ErrorBlock message={promoteAction.error} />
                  </div>
                ) : null}

                {promoteAction.receipt ? (
                  <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--panel-sunken)] px-3 py-2">
                    <span className="eyebrow text-[var(--accent)]">Nursery receipt</span>
                    <StatusPill
                      value={promoteAction.receipt.promoted ? "PROMOTED" : "REJECTED"}
                      tone={promoteAction.receipt.promoted ? "ok" : "bad"}
                    />
                    <span className="mono tnum text-[11px] text-[var(--ink-soft)]">
                      {int(promoteAction.receipt.shadow_fills)} / {int(promoteAction.receipt.required_fills)} shadow
                      fills
                    </span>
                    <div className="w-[120px]">
                      <InlineBar
                        value={promoteAction.receipt.shadow_fills}
                        max={promoteAction.receipt.required_fills || 30}
                        tone={promoteAction.receipt.graduated ? "pos" : "accent"}
                        label="Nursery shadow fill progress"
                      />
                    </div>
                    <span className="mono tnum text-[10px] text-[var(--dim)]">
                      {int(promoteAction.receipt.remaining_fills)} remaining
                    </span>
                    <StatusPill
                      value={promoteAction.receipt.graduated ? "GRADUATED" : "IN NURSERY"}
                      tone={promoteAction.receipt.graduated ? "ok" : "accent"}
                    />
                    <CopyHash
                      value={promoteAction.receipt.grounding_sha256}
                      label="copy nursery promotion digest"
                    />
                    <button
                      type="button"
                      className="btn btn-sm btn-ghost"
                      onClick={() => navigate(pagePath("analyzer", workspace))}
                      aria-label="Open the shadow nursery analyzer"
                    >
                      Open nursery
                    </button>
                  </div>
                ) : null}

                <div className="mt-3 grid-auto-180">
                  <MetricTile
                    label="Deflated Sharpe"
                    value={<TabularValue value={receipt.deflated_sharpe_ratio} digits={2} />}
                    hint={`gate ${num(dsrGate, 2)}`}
                    tone={receipt.passes_dsr_gate ? "pos" : "neg"}
                  />
                  <MetricTile
                    label="Net edge"
                    value={<TabularValue value={receipt.net_edge_bps} kind="bps" signTone />}
                    hint={`floor +${num(netFloor, 2)} bps`}
                    tone={receipt.passes_champion_floor ? "pos" : "neg"}
                  />
                  <MetricTile
                    label="Purged K-Fold"
                    value={`K = ${int(purged?.k)}`}
                    hint={`${int(purged?.fold_count)} folds · ${int(purged?.trades_in_folds)} out-of-sample trades`}
                  />
                  <MetricTile
                    label="Embargo"
                    value={<TabularValue value={(purged?.embargo_pct ?? 0) * 100} kind="pct" />}
                    hint={`${int(purged?.embargo_bars)} embargoed bars`}
                  />
                  <MetricTile
                    label="Leakage"
                    value={
                      <StatusPill
                        value={purged?.leakage_free === true ? "LEAKAGE FREE" : "LEAKAGE RISK"}
                        tone={passTone(purged?.leakage_free === true)}
                      />
                    }
                    hint="purged and embargoed folds"
                  />
                  <MetricTile
                    label="Out-of-sample net"
                    value={<TabularValue value={purged?.out_of_sample_net_bps} kind="bps" signTone />}
                    hint="weighted across folds"
                  />
                </div>
              </GlassPanel>

              <GlassPanel
                eyebrow="Forward accuracy"
                title="Horizon matrix"
                actions={<StatusPill value={`${int(horizonRows.length)} horizons`} tone="accent" />}
              >
                {horizonRows.length === 0 ? (
                  <p className="text-[11px] text-[var(--dim)]">
                    No forward sample was available on this tape.
                  </p>
                ) : (
                  <div className="scroll-x">
                    <table className="table table-compact" aria-label="Forward horizon accuracy">
                      <thead>
                        <tr>
                          <th scope="col">Horizon</th>
                          <th scope="col" className="num">
                            Samples
                          </th>
                          <th scope="col" className="num">
                            Wins
                          </th>
                          <th scope="col" className="num">
                            Win rate
                          </th>
                          <th scope="col" className="num">
                            Net edge
                          </th>
                          <th scope="col" className="num">
                            Cost
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {horizonRows.map(({ key, row }) => (
                          <tr key={key}>
                            <td>
                              <span className="mono tnum text-[11px]">T+{int(row.horizon_bars)}</span>
                            </td>
                            <td className="num">{int(row.samples)}</td>
                            <td className="num">{int(row.wins)}</td>
                            <td className="num">
                              <TabularValue value={row.win_rate_pct} kind="pct" />
                            </td>
                            <td className="num">
                              <TabularValue value={row.net_edge_bps} kind="bps" signTone />
                            </td>
                            <td className="num">
                              <TabularValue value={row.cost_bps} kind="bps" />
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </GlassPanel>

              <GlassPanel
                eyebrow="Trade ledger"
                title={`Trade-by-trade blotter · ${int(receipt.trade_count)} trades`}
                actions={<StatusPill value={receipt.symbol} tone="neutral" />}
              >
                {trades.length === 0 ? (
                  <p className="text-[11px] text-[var(--dim)]">
                    No trade triggered on this tape. The cost floor is still deducted from every signal before it is
                    scored.
                  </p>
                ) : (
                  <div>
                    {trades.length < receipt.trade_count ? (
                      <p className="mono tnum mb-1.5 text-[9.5px] text-[var(--dim)]">
                        showing the first {int(trades.length)} of {int(receipt.trade_count)} trades sealed in the
                        receipt
                      </p>
                    ) : null}
                    <div className="scroll-x scroll-y max-h-[420px]">
                      <table className="table table-compact zebra" aria-label="Trade-by-trade blotter">
                      <thead>
                        <tr>
                          <th scope="col">Trade</th>
                          <th scope="col">Side</th>
                          <th scope="col" className="num">
                            Entry
                          </th>
                          <th scope="col" className="num">
                            Exit
                          </th>
                          <th scope="col" className="num">
                            Gross
                          </th>
                          <th scope="col" className="num">
                            Cost
                          </th>
                          <th scope="col" className="num">
                            Net
                          </th>
                          <th scope="col" className="num">
                            Bars
                          </th>
                          <th scope="col">Reason</th>
                        </tr>
                      </thead>
                      <tbody>
                        {trades.map((trade, index) => (
                          <tr key={trade.trade_id || `${trade.entry_bar}-${index}`}>
                            <td>
                              <span className="mono text-[10px] text-[var(--ink)]" title={trade.trade_id}>
                                {trade.trade_id ? shortId(trade.trade_id, 10) : `#${index + 1}`}
                              </span>
                            </td>
                            <td>
                              <StatusPill value={trade.side} tone={sideTone(trade.side)} />
                            </td>
                            <td className="num">
                              <TabularValue value={trade.entry_price} kind="price" />
                            </td>
                            <td className="num">
                              <TabularValue value={trade.exit_price} kind="price" />
                            </td>
                            <td className="num">
                              <TabularValue value={trade.gross_bps} kind="bps" signTone />
                            </td>
                            <td className="num">
                              <TabularValue value={trade.cost_bps} kind="bps" />
                            </td>
                            <td className="num">
                              <TabularValue value={trade.net_bps} kind="bps" signTone />
                            </td>
                            <td className="num">{int(trade.bars_held)}</td>
                            <td>
                              <span className="text-[10.5px] text-[var(--dim)]">{text(trade.reason, "—")}</span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    </div>
                  </div>
                )}
              </GlassPanel>
            </>
          ) : (
            <GlassPanel eyebrow="Certification" title="Tax-net walk-forward">
              <EmptyCleanSlate
                title="No walk-forward run in this workspace"
                body="Each run replays a synthetic tape, subtracts the selected silo's cost floor and statutory tax before a trade is scored, then certifies the Deflated Sharpe across purged K-folds with an embargo. Nothing reaches the nursery until both gates clear."
                action={
                  floorRows.length > 0 ? (
                    <div className="chip-row justify-center">
                      {floorRows.map((row) => (
                        <span
                          key={row.silo}
                          className="pill"
                          title={`${row.silo}: ${num(row.cost_floor_bps, 2)} bps cost floor plus ${num(
                            row.statutory_tax_bps,
                            2,
                          )} bps statutory tax on every trade`}
                        >
                          <span className="mono">{row.silo}</span>
                          <span className="mono tnum text-[var(--accent)]">
                            {num(row.cost_floor_bps, 2)} bps
                          </span>
                          {row.statutory_tax_bps > 0 ? (
                            <span className="mono tnum text-[var(--warn)]">
                              +{num(row.statutory_tax_bps, 2)} tax
                            </span>
                          ) : null}
                        </span>
                      ))}
                    </div>
                  ) : undefined
                }
              />
            </GlassPanel>
          )}

          <GlassPanel
            eyebrow={
              comparison
                ? `8-strategy matrix · ${comparison.silo} · ${comparison.symbol} · ${comparison.timeframe}`
                : "8-strategy matrix"
            }
            title="Comparison leaderboard"
            actions={
              comparison ? (
                <StatusPill
                  value={`${int(comparison.strategy_count)} strategies`}
                  tone="accent"
                />
              ) : undefined
            }
          >
            {compareAction.error ? (
              <div className="mb-2">
                <ErrorBlock message={compareAction.error} />
              </div>
            ) : null}
            {leaderboard.length === 0 ? (
              <EmptyCleanSlate
                title="No comparison on this tape yet"
                body="Run all eight harvested strategies over one identical bar series. Every row is certified against the same DSR gate and silo cost floor."
                action={
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={() => void submitCompare()}
                    disabled={compareAction.busy}
                    aria-label="Compare all eight strategies on this tape"
                  >
                    {compareAction.busy ? "Comparing…" : "Compare All 8 Strategies"}
                  </button>
                }
              />
            ) : (
              <div className="scroll-x">
                <table className="table table-compact" aria-label="Eight-strategy comparison leaderboard">
                  <thead>
                    <tr>
                      <th scope="col" className="num">
                        Rank
                      </th>
                      <th scope="col">Strategy</th>
                      <th scope="col" className="num">
                        Return
                      </th>
                      <th scope="col" className="num">
                        Sharpe
                      </th>
                      <th scope="col" className="num">
                        Deflated Sharpe
                      </th>
                      <th scope="col" className="num">
                        Net edge
                      </th>
                      <th scope="col" className="num">
                        Win rate
                      </th>
                      <th scope="col" className="num">
                        Trades
                      </th>
                      <th scope="col" className="num">
                        Max DD
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {leaderboard.map((row) => (
                      <tr key={row.strategy_id}>
                        <td className="num">{int(row.rank)}</td>
                        <td>
                          <span className="mono text-[11px] text-[var(--ink)]">{row.strategy_id}</span>
                          <div className="text-[9.5px] text-[var(--dim)]">{row.strategy_name}</div>
                        </td>
                        <td className="num">
                          <TabularValue value={row.total_return_pct} kind="pct" signTone />
                        </td>
                        <td className="num">
                          <TabularValue value={row.sharpe_ratio} digits={2} signTone />
                        </td>
                        <td className="num">
                          <div className="flex items-center justify-end gap-1.5">
                            <TabularValue value={row.deflated_sharpe_ratio} digits={2} />
                            <StatusPill
                              value={row.passes_dsr_gate ? "PASS" : "FAIL"}
                              tone={passTone(row.passes_dsr_gate)}
                            />
                          </div>
                        </td>
                        <td className="num">
                          <TabularValue value={row.net_edge_bps} kind="bps" signTone />
                        </td>
                        <td className="num">
                          <TabularValue value={row.win_rate_pct} kind="pct" />
                        </td>
                        <td className="num">{int(row.total_trades)}</td>
                        <td className="num">
                          <TabularValue value={row.max_drawdown_pct} kind="pct" />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </GlassPanel>

          <GlassPanel
            eyebrow="History"
            title="Saved walk-forward runs"
            actions={
              <StatusPill
                value={data.clean_slate ? "clean slate" : `${int(data.run_count)} saved`}
                tone={data.clean_slate ? "neutral" : "accent"}
              />
            }
          >
            {runs.length === 0 ? (
              <EmptyCleanSlate
                title="No saved run yet"
                body="Every certified run is kept with its grounding digest. Run the tape above to start the history."
              />
            ) : (
              <div className="scroll-x">
                <table className="table table-compact" aria-label="Saved walk-forward runs">
                  <thead>
                    <tr>
                      <th scope="col">Run</th>
                      <th scope="col">Tape</th>
                      <th scope="col">Strategy</th>
                      <th scope="col" className="num">
                        Deflated Sharpe
                      </th>
                      <th scope="col" className="num">
                        Net edge
                      </th>
                      <th scope="col">Gate</th>
                      <th scope="col">Created</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((run) => (
                      <tr key={run.backtest_id}>
                        <td>
                          <span className="mono text-[10px] text-[var(--ink)]" title={run.backtest_id}>
                            {shortId(run.backtest_id, 12)}
                          </span>
                        </td>
                        <td>
                          <span className="mono text-[10px] text-[var(--ink-soft)]">
                            {run.silo} · {run.symbol}
                          </span>
                        </td>
                        <td>
                          <span className="text-[10.5px] text-[var(--ink-soft)]">
                            {text(run.strategy_name, run.strategy_id)}
                          </span>
                        </td>
                        <td className="num">
                          <TabularValue value={run.deflated_sharpe_ratio} digits={2} />
                        </td>
                        <td className="num">
                          <TabularValue value={run.net_edge_bps} kind="bps" signTone />
                        </td>
                        <td>
                          <StatusPill
                            value={run.promotable ? "PROMOTABLE" : "BLOCKED"}
                            tone={passTone(run.promotable)}
                          />
                        </td>
                        <td className="num">
                          <span className="mono tnum text-[10px]" title={utc(run.created_at_ns)}>
                            {ago(run.created_at_ns)}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </GlassPanel>
        </>
      ) : null}
    </div>
  );
}
