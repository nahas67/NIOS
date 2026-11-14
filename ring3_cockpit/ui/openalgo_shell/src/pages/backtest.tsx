import { Activity, ArrowUpRight, BarChart3, ShieldCheck, TrendingDown } from 'lucide-react';
import { useState } from 'react';
import { ActionFeedback, ActionPanel, ProofRow } from '@/components/operator-action';
import { DataState, PageShell, StatTile } from '@/components/page-shell';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useAction } from '@/hooks/use-action';
import { usePageData } from '@/hooks/use-page-data';
import {
  fetchBacktestPage,
  postPromoteToNursery,
  postRunBacktest,
} from '@/lib/api';
import { cell, num, rowKey } from '@/lib/rows';
import type {
  BacktestPageData,
  BacktestReceipt,
  NurseryPromotionReceipt,
} from '@/types';

const SILO_IDS = ['CRYPTO_BINANCE', 'INDIA_ZERODHA_DHAN', 'US_IBKR', 'FX_MT5'] as const;
const HORIZONS = ['t_plus_1', 't_plus_5', 't_plus_20'] as const;

const HORIZON_LABELS: Record<string, string> = {
  t_plus_1: 'T+1',
  t_plus_5: 'T+5',
  t_plus_20: 'T+20',
};

function fmt(value: unknown, digits = 2): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—';
}

function buildPath(values: number[]): string {
  if (values.length < 2) return '';
  let min = values[0];
  let max = values[0];
  for (const value of values) {
    if (value < min) min = value;
    if (value > max) max = value;
  }
  const span = max - min || 1;
  const step = 100 / (values.length - 1);
  return values
    .map((value, index) => {
      const x = index * step;
      const y = 40 - ((value - min) / span) * 36 - 2;
      return `${index === 0 ? 'M' : 'L'}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(' ');
}

function buildArea(values: number[]): string {
  const line = buildPath(values);
  return line ? `${line} L100,40 L0,40 Z` : '';
}

export function BacktestPage() {
  const page = usePageData(fetchBacktestPage);
  const data: BacktestPageData | null = page.data;

  const [silo, setSilo] = useState<string>('INDIA_ZERODHA_DHAN');
  const [symbol, setSymbol] = useState<string>('RELIANCE');
  const [strategyId, setStrategyId] = useState<string>('ma_golden_cross');
  const [bars, setBars] = useState<string>('252');
  const [capital, setCapital] = useState<string>('100000');
  const [fastWindow, setFastWindow] = useState<string>('6');
  const [slowWindow, setSlowWindow] = useState<string>('40');
  const [stopBps, setStopBps] = useState<string>('300');
  const [takeBps, setTakeBps] = useState<string>('900');

  const [receipt, setReceipt] = useState<BacktestReceipt | null>(null);
  const [promotion, setPromotion] = useState<NurseryPromotionReceipt | null>(null);

  const run = useAction<BacktestReceipt>();
  const promote = useAction<NurseryPromotionReceipt>();

  const strategies = data?.strategies ?? [];
  const runs = data?.runs ?? [];
  const runCostFloor =
    typeof receipt?.cost_floor_bps === 'number'
      ? receipt.cost_floor_bps
      : (data?.cost_floors?.[silo] ?? null);
  const promotable =
    receipt?.passes_dsr_gate === true &&
    receipt?.passes_champion_floor === true &&
    !promotion?.promoted;

  const metrics = receipt?.metrics ?? {};
  const kfold = receipt?.purged_kfold ?? null;

  async function runBacktest() {
    const parsedBars = Number.parseInt(bars, 10);
    const payload = {
      silo,
      symbol: symbol.trim().toUpperCase(),
      strategy_id: strategyId,
      bars: Number.isFinite(parsedBars) ? parsedBars : 252,
      initial_capital_units: Number.parseFloat(capital) || 100000,
      fast_window: Number.parseInt(fastWindow, 10) || null,
      slow_window: Number.parseInt(slowWindow, 10) || null,
      stop_loss_bps: Number.parseFloat(stopBps) || null,
      take_profit_bps: Number.parseFloat(takeBps) || null,
    };
    const result = await run.run(() => postRunBacktest(payload));
    if (result) {
      setReceipt(result);
      setPromotion(null);
      page.reload();
    }
  }

  async function promoteRun() {
    if (!receipt) return;
    const result = await promote.run(() => postPromoteToNursery(receipt.backtest_id));
    if (result) {
      setPromotion(result);
      page.reload();
    }
  }

  return (
    <PageShell
      title="Walk-Forward Backtester"
      ring="RING 2"
      capability="PAPER CONTROL"
      subtitle="Deterministic tax-net walk-forward backtests with Purged K-Fold cross-validation, embargo, and the Deflated Sharpe promotion gate. A passing run enters the 30-fill shadow nursery — never live."
      actions={
        <div className="flex items-center gap-2">
          <Badge variant="outline" className="font-mono text-[10px]">
            DSR GATE {fmt(data?.dsr_gate ?? 1.5)}
          </Badge>
          <Badge variant="outline" className="font-mono text-[10px]">
            K={data?.k_folds ?? 5} · EMBARGO {fmt((data?.embargo_pct ?? 0.02) * 100)}%
          </Badge>
        </div>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="Saved Runs"
          value={String(data?.run_count ?? 0)}
          hint={data?.clean_slate ? 'Clean slate' : 'Grounded receipts'}
          icon={BarChart3}
        />
        <StatTile
          label="Silo Cost Floor"
          value={`${fmt(runCostFloor)} bps`}
          hint={`${silo} statutory + slippage`}
          icon={TrendingDown}
        />
        <StatTile
          label="Deflated Sharpe"
          value={fmt(metrics.deflated_sharpe_ratio)}
          hint={receipt ? (receipt.passes_dsr_gate ? 'Gate passed' : 'Below gate') : 'No run yet'}
          icon={ShieldCheck}
        />
        <StatTile
          label="Net Edge"
          value={`${fmt(metrics.net_edge_bps)} bps`}
          hint={`Floor ${fmt(data?.champion_net_edge_floor_bps ?? 0.5)} bps`}
          icon={Activity}
        />
      </div>

      <DataState loading={page.loading} error={page.error}>
        <ActionPanel
          title="Backtest Configuration"
          description="Cost floors are applied on every trade before any edge is reported."
        >
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <div className="grid gap-1.5">
              <Label htmlFor="bt-silo">Silo</Label>
              <Select value={silo} onValueChange={setSilo}>
                <SelectTrigger id="bt-silo" className="h-8 font-mono text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SILO_IDS.map((entry) => (
                    <SelectItem key={entry} value={entry} className="font-mono text-xs">
                      {entry} · {fmt(data?.cost_floors?.[entry])} bps
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="grid gap-1.5">
              <Label htmlFor="bt-symbol">Symbol</Label>
              <Input
                id="bt-symbol"
                className="h-8 font-mono text-xs"
                value={symbol}
                onChange={(event) => setSymbol(event.target.value)}
              />
            </div>

            <div className="grid gap-1.5">
              <Label htmlFor="bt-strategy">Strategy</Label>
              <Select value={strategyId} onValueChange={setStrategyId}>
                <SelectTrigger id="bt-strategy" className="h-8 font-mono text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(strategies.length > 0
                    ? strategies.map((entry) => entry.strategy_id)
                    : [...Array(0)].map(() => '')
                  ).map((entry) => (
                    <SelectItem key={entry} value={entry} className="font-mono text-xs">
                      {entry}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="grid gap-1.5">
              <Label htmlFor="bt-bars">Bar Count (60–1000)</Label>
              <Input
                id="bt-bars"
                className="h-8 font-mono text-xs"
                value={bars}
                onChange={(event) => setBars(event.target.value)}
              />
            </div>

            <div className="grid gap-1.5">
              <Label htmlFor="bt-capital">Starting Capital</Label>
              <Input
                id="bt-capital"
                className="h-8 font-mono text-xs"
                value={capital}
                onChange={(event) => setCapital(event.target.value)}
              />
            </div>

            <div className="grid gap-1.5">
              <Label htmlFor="bt-fast">Fast MA</Label>
              <Input
                id="bt-fast"
                className="h-8 font-mono text-xs"
                value={fastWindow}
                onChange={(event) => setFastWindow(event.target.value)}
              />
            </div>

            <div className="grid gap-1.5">
              <Label htmlFor="bt-slow">Slow MA</Label>
              <Input
                id="bt-slow"
                className="h-8 font-mono text-xs"
                value={slowWindow}
                onChange={(event) => setSlowWindow(event.target.value)}
              />
            </div>

            <div className="grid gap-1.5">
              <Label htmlFor="bt-stop">Stop Loss (bps)</Label>
              <Input
                id="bt-stop"
                className="h-8 font-mono text-xs"
                value={stopBps}
                onChange={(event) => setStopBps(event.target.value)}
              />
            </div>

            <div className="grid gap-1.5">
              <Label htmlFor="bt-take">Take Profit (bps)</Label>
              <Input
                id="bt-take"
                className="h-8 font-mono text-xs"
                value={takeBps}
                onChange={(event) => setTakeBps(event.target.value)}
              />
            </div>

            <div className="flex items-end">
              <Button type="button" size="sm" disabled={run.pending} onClick={runBacktest}>
                <Activity className="mr-1 size-3.5" />
                {run.pending ? 'Running…' : 'Run Walk-Forward Backtest'}
              </Button>
            </div>
          </div>
          <ActionFeedback pending={run.pending} error={run.error} />
        </ActionPanel>

        {receipt ? (
          <>
            <Card>
              <CardHeader className="pb-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <CardTitle className="text-sm">
                    Performance &amp; Promotion Gate · {receipt.strategy_id} · {receipt.symbol}
                  </CardTitle>
                  <div className="flex items-center gap-1.5">
                    <Badge
                      variant="outline"
                      className={`font-mono text-[10px] ${
                        receipt.passes_dsr_gate
                          ? 'border-emerald-500/40 text-emerald-300'
                          : 'border-rose-500/40 text-rose-300'
                      }`}
                    >
                      DSR {receipt.passes_dsr_gate ? 'PASS' : 'FAIL'}
                    </Badge>
                    <Badge
                      variant="outline"
                      className={`font-mono text-[10px] ${
                        receipt.passes_champion_floor
                          ? 'border-emerald-500/40 text-emerald-300'
                          : 'border-amber-500/40 text-amber-300'
                      }`}
                    >
                      NET EDGE {receipt.passes_champion_floor ? 'PASS' : 'FAIL'}
                    </Badge>
                    <Badge
                      variant="outline"
                      className={`font-mono text-[10px] ${
                        receipt.verified
                          ? 'border-sky-500/40 text-sky-300'
                          : 'border-rose-500/40 text-rose-300'
                      }`}
                    >
                      {receipt.verified ? 'GROUNDED' : 'UNGROUNDED'}
                    </Badge>
                  </div>
                </div>
              </CardHeader>
              <CardContent className="grid gap-3">
                <div className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-4">
                  <ProofRow label="total_return_pct" value={fmt(metrics.total_return_pct)} />
                  <ProofRow
                    label="benchmark_return_pct"
                    value={fmt(metrics.benchmark_return_pct)}
                  />
                  <ProofRow label="alpha_pct" value={fmt(metrics.alpha_pct)} />
                  <ProofRow label="cagr_pct" value={fmt(metrics.cagr_pct)} />
                  <ProofRow label="sharpe_ratio" value={fmt(metrics.sharpe_ratio)} />
                  <ProofRow
                    label="deflated_sharpe_ratio"
                    value={fmt(metrics.deflated_sharpe_ratio)}
                  />
                  <ProofRow label="sortino_ratio" value={fmt(metrics.sortino_ratio)} />
                  <ProofRow label="calmar_ratio" value={fmt(metrics.calmar_ratio)} />
                  <ProofRow label="max_drawdown_pct" value={fmt(metrics.max_drawdown_pct)} />
                  <ProofRow label="win_rate_pct" value={fmt(metrics.win_rate_pct)} />
                  <ProofRow label="profit_factor" value={fmt(metrics.profit_factor)} />
                  <ProofRow label="total_trades" value={fmt(metrics.total_trades, 0)} />
                  <ProofRow label="gross_edge_bps" value={fmt(metrics.gross_edge_bps)} />
                  <ProofRow
                    label="tax_and_cost_drag_bps"
                    value={fmt(metrics.tax_and_cost_drag_bps)}
                  />
                  <ProofRow label="net_edge_bps" value={fmt(metrics.net_edge_bps)} />
                  <ProofRow label="cost_floor_bps" value={fmt(receipt.cost_floor_bps)} />
                </div>

                {kfold ? (
                  <div className="rounded-md border border-border/60 p-2">
                    <p className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
                      Purged K-Fold Cross-Validation
                    </p>
                    <div className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-5">
                      <ProofRow label="folds" value={fmt(kfold.fold_count, 0)} />
                      <ProofRow label="embargo_bars" value={fmt(kfold.embargo_bars, 0)} />
                      <ProofRow label="purged" value={fmt(kfold.purged, 0)} />
                      <ProofRow label="oob_net_bps" value={fmt(kfold.out_of_sample_net_bps)} />
                      <ProofRow
                        label="leakage_free"
                        value={kfold.leakage_free ? 'true' : 'false'}
                      />
                    </div>
                  </div>
                ) : null}

                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    type="button"
                    size="sm"
                    disabled={!promotable || promote.pending}
                    onClick={promoteRun}
                  >
                    <ArrowUpRight className="mr-1 size-3.5" />
                    Promote to 30-Fill Shadow Nursery
                  </Button>
                  <span className="text-[11px] text-muted-foreground">
                    Requires DSR ≥ {fmt(data?.dsr_gate ?? 1.5)} and net edge ≥{' '}
                    {fmt(data?.champion_net_edge_floor_bps ?? 0.5)} bps.
                  </span>
                  {promotion?.promoted ? (
                    <Badge
                      variant="outline"
                      className="border-emerald-500/40 font-mono text-[10px] text-emerald-300"
                    >
                      NURSERY {promotion.shadow_fills}/{promotion.required_fills} ·{' '}
                      {promotion.plugin_name}
                    </Badge>
                  ) : null}
                </div>
                <ActionFeedback pending={promote.pending} error={promote.error} />
                <ProofRow label="grounding_sha256" value={receipt.grounding_sha256} />
              </CardContent>
            </Card>

            <div className="grid gap-4 xl:grid-cols-2">
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">Equity Curve (NAV units)</CardTitle>
                </CardHeader>
                <CardContent>
                  <svg
                    viewBox="0 0 100 40"
                    preserveAspectRatio="none"
                    className="h-40 w-full"
                    aria-hidden="true"
                    focusable="false"
                  >
                    <path
                      d={buildArea(receipt.equity_curve)}
                      className="fill-sky-500/10"
                      stroke="none"
                    />
                    <path
                      d={buildPath(receipt.equity_curve)}
                      className="stroke-sky-400"
                      fill="none"
                      strokeWidth={0.6}
                      vectorEffect="non-scaling-stroke"
                    />
                    <path
                      d={buildPath(receipt.benchmark_curve_units)}
                      className="stroke-muted-foreground/70"
                      fill="none"
                      strokeWidth={0.5}
                      strokeDasharray="1.5 1.5"
                      vectorEffect="non-scaling-stroke"
                    />
                  </svg>
                  <div className="mt-2 grid grid-cols-3 gap-x-4">
                    <ProofRow label="final_nav" value={fmt(receipt.final_nav_units)} />
                    <ProofRow label="benchmark_nav" value={fmt(receipt.benchmark_final_units)} />
                    <ProofRow label="bars" value={fmt(receipt.bars, 0)} />
                  </div>
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">Drawdown Curve (%)</CardTitle>
                </CardHeader>
                <CardContent>
                  <svg
                    viewBox="0 0 100 40"
                    preserveAspectRatio="none"
                    className="h-40 w-full"
                    aria-hidden="true"
                    focusable="false"
                  >
                    <path
                      d={buildArea(receipt.drawdown_curve_pct)}
                      className="fill-rose-500/10"
                      stroke="none"
                    />
                    <path
                      d={buildPath(receipt.drawdown_curve_pct)}
                      className="stroke-rose-400"
                      fill="none"
                      strokeWidth={0.6}
                      vectorEffect="non-scaling-stroke"
                    />
                  </svg>
                  <div className="mt-2 grid grid-cols-3 gap-x-4">
                    <ProofRow label="max_dd_pct" value={fmt(metrics.max_drawdown_pct)} />
                    <ProofRow label="trades" value={fmt(receipt.trade_count, 0)} />
                    <ProofRow label="signal_bars" value={fmt(receipt.signal_bars, 0)} />
                  </div>
                </CardContent>
              </Card>
            </div>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">
                  Signal Horizon Accuracy Matrix (T+1 · T+5 · T+20)
                </CardTitle>
              </CardHeader>
              <CardContent className="overflow-x-auto">
                <table className="w-full min-w-[32rem] border-collapse text-left font-mono text-[11px]">
                  <thead>
                    <tr className="border-b border-border/60 text-muted-foreground">
                      <th className="py-1.5 pr-3 font-normal">horizon</th>
                      <th className="py-1.5 pr-3 text-right font-normal">samples</th>
                      <th className="py-1.5 pr-3 text-right font-normal">wins</th>
                      <th className="py-1.5 pr-3 text-right font-normal">win_rate_pct</th>
                      <th className="py-1.5 pr-3 text-right font-normal">net_edge_bps</th>
                      <th className="py-1.5 text-right font-normal">cost_bps</th>
                    </tr>
                  </thead>
                  <tbody>
                    {HORIZONS.map((key) => {
                      const row = receipt.horizon_matrix[key];
                      return (
                        <tr key={key} className="border-b border-border/30">
                          <td className="py-1.5 pr-3">{HORIZON_LABELS[key]}</td>
                          <td className="py-1.5 pr-3 text-right">{fmt(row?.samples, 0)}</td>
                          <td className="py-1.5 pr-3 text-right">{fmt(row?.wins, 0)}</td>
                          <td className="py-1.5 pr-3 text-right">{fmt(row?.win_rate_pct)}</td>
                          <td className="py-1.5 pr-3 text-right">{fmt(row?.net_edge_bps)}</td>
                          <td className="py-1.5 text-right">{fmt(row?.cost_bps)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Trade Blotter</CardTitle>
              </CardHeader>
              <CardContent className="overflow-x-auto">
                <table className="w-full min-w-[48rem] border-collapse text-left font-mono text-[11px]">
                  <thead>
                    <tr className="border-b border-border/60 text-muted-foreground">
                      <th className="py-1.5 pr-3 font-normal">side</th>
                      <th className="py-1.5 pr-3 text-right font-normal">entry_bar</th>
                      <th className="py-1.5 pr-3 text-right font-normal">exit_bar</th>
                      <th className="py-1.5 pr-3 text-right font-normal">entry_price</th>
                      <th className="py-1.5 pr-3 text-right font-normal">exit_price</th>
                      <th className="py-1.5 pr-3 text-right font-normal">gross_bps</th>
                      <th className="py-1.5 pr-3 text-right font-normal">cost_bps</th>
                      <th className="py-1.5 pr-3 text-right font-normal">net_bps</th>
                      <th className="py-1.5 font-normal">reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {receipt.trades.length === 0 ? (
                      <tr>
                        <td className="py-3 text-muted-foreground" colSpan={9}>
                          No trades were triggered on this tape.
                        </td>
                      </tr>
                    ) : (
                      receipt.trades.map((row, index) => (
                        <tr key={rowKey(row, index)} className="border-b border-border/30">
                          <td className="py-1.5 pr-3">{cell(row, 'side')}</td>
                          <td className="py-1.5 pr-3 text-right">{fmt(num(row, 'entry_bar'), 0)}</td>
                          <td className="py-1.5 pr-3 text-right">{fmt(num(row, 'exit_bar'), 0)}</td>
                          <td className="py-1.5 pr-3 text-right">
                            {fmt(num(row, 'entry_price'))}
                          </td>
                          <td className="py-1.5 pr-3 text-right">{fmt(num(row, 'exit_price'))}</td>
                          <td className="py-1.5 pr-3 text-right">{fmt(num(row, 'gross_bps'))}</td>
                          <td className="py-1.5 pr-3 text-right">{fmt(num(row, 'cost_bps'))}</td>
                          <td
                            className={`py-1.5 pr-3 text-right ${
                              num(row, 'net_bps') >= 0 ? 'text-emerald-300' : 'text-rose-300'
                            }`}
                          >
                            {fmt(num(row, 'net_bps'))}
                          </td>
                          <td className="py-1.5">{cell(row, 'reason')}</td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </CardContent>
            </Card>
          </>
        ) : (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">Saved Backtest Runs</CardTitle>
            </CardHeader>
            <CardContent className="overflow-x-auto">
              <table className="w-full min-w-[46rem] border-collapse text-left font-mono text-[11px]">
                <thead>
                  <tr className="border-b border-border/60 text-muted-foreground">
                    <th className="py-1.5 pr-3 font-normal">backtest_id</th>
                    <th className="py-1.5 pr-3 font-normal">silo</th>
                    <th className="py-1.5 pr-3 font-normal">symbol</th>
                    <th className="py-1.5 pr-3 font-normal">strategy_id</th>
                    <th className="py-1.5 pr-3 text-right font-normal">dsr</th>
                    <th className="py-1.5 pr-3 text-right font-normal">net_bps</th>
                    <th className="py-1.5 font-normal">gate</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.length === 0 ? (
                    <tr>
                      <td className="py-3 text-muted-foreground" colSpan={7}>
                        Clean slate — no walk-forward backtests have been run in this workspace.
                      </td>
                    </tr>
                  ) : (
                    runs.map((row, index) => (
                      <tr key={rowKey(row, index)} className="border-b border-border/30">
                        <td className="py-1.5 pr-3">{cell(row, 'backtest_id')}</td>
                        <td className="py-1.5 pr-3">{cell(row, 'silo')}</td>
                        <td className="py-1.5 pr-3">{cell(row, 'symbol')}</td>
                        <td className="py-1.5 pr-3">{cell(row, 'strategy_id')}</td>
                        <td className="py-1.5 pr-3 text-right">
                          {fmt(num(row, 'deflated_sharpe_ratio'))}
                        </td>
                        <td className="py-1.5 pr-3 text-right">{fmt(num(row, 'net_edge_bps'))}</td>
                        <td className="py-1.5">
                          {row.passes_dsr_gate && row.passes_champion_floor
                            ? 'PROMOTABLE'
                            : 'BELOW GATE'}
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </CardContent>
          </Card>
        )}
      </DataState>
    </PageShell>
  );
}
