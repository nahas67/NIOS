/**
 * Walk-forward backtester.
 *
 * Every run is tax-net and purged-K-fold certified. A result that cannot clear
 * the cost floor is reported as a rejection, not quietly displayed as a win.
 */

import { useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { num, rows, str, titleCase } from "@/lib/format";
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
import { EquityDrawdownChart } from "@/charts/EquityDrawdownChart";
import { RadialGauge } from "@/charts/RadialGauge";

export function BacktestPage(): ReactNode {
  const page = useApi(() => api.backtest(), []);
  const [silo, setSilo] = useState("INDIA");
  const [symbol, setSymbol] = useState("RELIANCE");
  const [strategy, setStrategy] = useState("MA_CROSS");
  const [bars, setBars] = useState("1500");
  const [busy, setBusy] = useState(false);
  const [receipt, setReceipt] = useState<Record<string, unknown> | null>(null);

  const runs = rows(page.data, "runs");
  const strategies = rows(page.data, "strategies");
  const thresholds = page.data?.thresholds;
  const dsr = num(receipt, "deflated_sharpe", num(receipt, "metrics.deflated_sharpe", 0));
  const netBps = num(receipt, "net_bps", num(receipt, "metrics.net_bps", 0));
  const trades = num(receipt, "trades", num(receipt, "metrics.trades", 0));
  const equityRaw = receipt?.equity_curve;
  const equity = Array.isArray(equityRaw) ? equityRaw.map((value) => Number(value) || 0) : [];
  const drawdownRaw = receipt?.drawdown_pct;
  const drawdown = Array.isArray(drawdownRaw) ? drawdownRaw.map((value) => Number(value) || 0) : [];

  const run = async () => {
    setBusy(true);
    try {
      const next = await api.runBacktest({
        silo,
        symbol: symbol.toUpperCase(),
        strategy_id: strategy,
        bars: Number(bars) || 1500,
        timeframe: "1m",
        initial_capital_units: 1_000_000,
      });
      setReceipt(next);
      toast(`Walk-forward run complete for ${symbol.toUpperCase()}`, "success");
      page.reload();
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "backtest rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="nx-page">
      <SectionHeader
        kicker="RING 2 - RESEARCH"
        title="Walk-Forward Backtester"
        sub="Purged K-fold, tax-net cost floor and a Deflated Sharpe promotion gate. A run below +0.50 bps is retired, not averaged in."
        actions={
          <>
            <StatusBadge tone="muted">DSR &gt;= 1.50 TO PROMOTE</StatusBadge>
            <Btn size="sm" tone="ghost" onClick={page.reload}>
              Reload catalogue
            </Btn>
          </>
        }
      />

      <div className="nx-grid nx-grid--4">
        <MetricTile label="Saved runs" value={String(runs.length)} sub="per workspace" />
        <MetricTile label="Strategies" value={String(strategies.length)} sub="catalogued candidates" />
        <MetricTile
          label="Deflated Sharpe"
          value={dsr ? dsr.toFixed(2) : "-"}
          sub=">= 1.50 to promote"
          tone={dsr >= 1.5 ? "bull" : "neutral"}
        />
        <MetricTile
          label="Net edge"
          value={netBps ? `${netBps.toFixed(1)} bps` : "-"}
          sub="after spread, fee and tax"
          tone={netBps > 0 ? "bull" : "bear"}
        />
      </div>

      <div className="nx-grid nx-grid--1-2">
        <TerminalPanel title="Run configuration" subtitle="POST /api/v2/control/backtest/run">
          <Field label="Silo">
            <select className="nx-select" value={silo} onChange={(event) => setSilo(event.target.value)}>
              <option value="INDIA">INDIA</option>
              <option value="US">US</option>
              <option value="FX">FX</option>
              <option value="CRYPTO">CRYPTO</option>
            </select>
          </Field>
          <Field label="Symbol">
            <input className="nx-input" value={symbol} onChange={(event) => setSymbol(event.target.value)} />
          </Field>
          <Field label="Strategy">
            <input className="nx-input" value={strategy} onChange={(event) => setStrategy(event.target.value)} />
          </Field>
          <Field label="Bars">
            <input className="nx-input" value={bars} onChange={(event) => setBars(event.target.value)} />
          </Field>
          <Btn tone="primary" block onClick={run} disabled={busy} style={{ marginTop: 8 }}>
            {busy ? "Running walk-forward..." : "Run backtest"}
          </Btn>
          <p className="nx-xs nx-muted" style={{ marginTop: 6 }}>
            Cost floor {num(thresholds, "cost_floor_bps", num(page.data, "cost_floor_bps", 0)).toFixed(1)} bps,
            minimum trades {num(thresholds, "min_trades", 30)}.
          </p>
        </TerminalPanel>

        <div className="nx-stack">
          <TerminalPanel title="Equity and drawdown" subtitle="Strategy versus benchmark, rebased to 100">
            <EquityDrawdownChart strategy={equity} drawdownPct={drawdown} />
          </TerminalPanel>
          <TerminalPanel title="Promotion gate" subtitle="A challenger needs thirty shadow fills too">
            <div className="nx-grid nx-grid--2">
              <RadialGauge value={dsr} max={3} label="DSR" unit="" tone={dsr >= 1.5 ? "bull" : "accent"} size={150} />
              <div className="nx-stack" style={{ justifyContent: "center" }}>
                <div className="nx-row nx-row--between">
                  <span className="nx-xs nx-muted">Trades</span>
                  <span className="nx-sm nx-strong">{trades}</span>
                </div>
                <div className="nx-row nx-row--between">
                  <span className="nx-xs nx-muted">Net edge</span>
                  <span className={netBps > 0 ? "nx-sm nx-bull nx-strong" : "nx-sm nx-bear nx-strong"}>
                    {netBps.toFixed(2)} bps
                  </span>
                </div>
                <div className="nx-row nx-row--between">
                  <span className="nx-xs nx-muted">Verdict</span>
                  <StatusBadge tone={dsr >= 1.5 ? "bull" : "muted"}>
                    {dsr >= 1.5 ? "PROMOTABLE" : "WITHHELD"}
                  </StatusBadge>
                </div>
              </div>
            </div>
          </TerminalPanel>
        </div>
      </div>

      <TerminalPanel title="Saved runs" subtitle="Chronological, never overwritten" flush>
        <DataGrid
          columns={[
            {
              key: "id",
              label: "Run",
              render: (row: Record<string, unknown>) => (
                <span className="nx-mono">{str(row, "backtest_id", str(row, "id", "-")).slice(0, 12)}</span>
              ),
            },
            {
              key: "symbol",
              label: "Symbol",
              render: (row: Record<string, unknown>) => <span className="nx-strong">{str(row, "symbol", "-")}</span>,
            },
            {
              key: "strategy",
              label: "Strategy",
              render: (row: Record<string, unknown>) => (
                <span className="nx-dim nx-xs">{str(row, "strategy_id", "-")}</span>
              ),
            },
            {
              key: "dsr",
              label: "DSR",
              align: "right",
              render: (row: Record<string, unknown>) => {
                const value = num(row, "deflated_sharpe", num(row, "dsr", 0));
                return <span className={value >= 1.5 ? "nx-bull nx-num" : "nx-num nx-dim"}>{value.toFixed(2)}</span>;
              },
            },
            {
              key: "state",
              label: "Verdict",
              align: "right",
              render: (row: Record<string, unknown>) => {
                const state = str(row, "verdict", str(row, "status", "WITHHELD")).toUpperCase();
                return <StatusBadge tone={state === "PROMOTED" ? "bull" : "muted"}>{titleCase(state)}</StatusBadge>;
              },
            },
          ]}
          rows={runs}
          getKey={(row, index) => str(row, "backtest_id", String(index))}
          empty={
            <EmptyCleanSlate
              title="No walk-forward run yet"
              body="The run catalogue starts empty. Launch a run above; results are appended, never mutated."
              hint="purged K-fold"
            />
          }
        />
      </TerminalPanel>
    </div>
  );
}
