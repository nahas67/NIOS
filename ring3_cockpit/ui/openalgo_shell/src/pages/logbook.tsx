import { useState } from 'react'
import { BookMarked, ClipboardList, Percent, RotateCcw, Scale, TrendingDown, TrendingUp } from 'lucide-react'

import { ActionFeedback, ActionPanel, ProofRow } from '@/components/operator-action'
import { DataState, PageShell, StatTile } from '@/components/page-shell'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useAction } from '@/hooks/use-action'
import { usePageData } from '@/hooks/use-page-data'
import { fetchLogbookPage, postLogbookClear, postLogbookEntry } from '@/lib/api'
import { formatClock, formatUsd } from '@/lib/format'
import type { LogbookEntryReceipt } from '@/types'

const SILOS = ['US_IBKR', 'INDIA_ZERODHA_DHAN', 'CRYPTO_BINANCE', 'FX_MT5'] as const
const SETUPS = [
  'ma_golden_cross',
  'bullish_trend_alignment',
  'chanlun_pivot_breakout',
  'elliott_wave_impulse',
  'event_catalyst_momentum',
  'growth_quality_compounder',
  'expectation_rerating',
  'cross_asset_etf_rotation',
] as const

function formatNumber(value: number, digits = 2): string {
  if (!Number.isFinite(value)) return '—'
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

export function LogbookPage() {
  const { data, error, loading, reload } = usePageData(fetchLogbookPage)
  const entryAction = useAction<LogbookEntryReceipt>()
  const clearAction = useAction<Record<string, unknown>>()

  const [silo, setSilo] = useState<string>('US_IBKR')
  const [symbol, setSymbol] = useState('SPY')
  const [side, setSide] = useState<'LONG' | 'SHORT'>('LONG')
  const [setup, setSetup] = useState<string>('ma_golden_cross')
  const [entryPrice, setEntryPrice] = useState('')
  const [exitPrice, setExitPrice] = useState('')
  const [quantity, setQuantity] = useState('')
  const [stopLoss, setStopLoss] = useState('')
  const [thesis, setThesis] = useState('')
  const [lessons, setLessons] = useState('')
  const [workspace, setWorkspace] = useState<'PAPER' | 'LIVE'>('PAPER')

  const entries = data?.entries ?? []
  const aggregate = data?.aggregate
  const counts = data?.counts

  async function record() {
    const receipt = await entryAction.run(() =>
      postLogbookEntry({
        silo,
        symbol,
        side,
        setup_strategy: setup,
        entry_price: Number(entryPrice),
        exit_price: Number(exitPrice),
        quantity: Number(quantity),
        stop_loss: stopLoss ? Number(stopLoss) : 0,
        thesis,
        lessons_learned: lessons,
        workspace,
      }),
    )
    if (receipt) {
      setEntryPrice('')
      setExitPrice('')
      setQuantity('')
      setStopLoss('')
      setThesis('')
      setLessons('')
      reload()
    }
  }

  async function clearJournal() {
    await clearAction.run(() => postLogbookClear())
    reload()
  }

  return (
    <PageShell
      title="Trade Logbook & Execution Journal"
      ring="RING 0"
      capability="PAPER CONTROL"
      subtitle="Every recorded round trip is cost-adjusted before attribution: gross P&L minus the silo's statutory cost floor in basis points, then net P&L, R-multiple and win/loss attribution. A clean-slate boot starts with zero entries."
      actions={
        <Badge variant="outline" className="font-mono text-[10px]">
          {counts?.entries ?? 0} ENTRIES · {data?.clean_slate ? 'CLEAN SLATE' : 'ACTIVE'}
        </Badge>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          icon={Scale}
          label="Net P&L"
          value={formatUsd(aggregate?.net_pnl ?? 0, 2)}
          hint={`gross ${formatUsd(aggregate?.gross_pnl ?? 0, 2)} · cost ${formatUsd(aggregate?.cost_drag ?? 0, 2)}`}
          accent={(aggregate?.net_pnl ?? 0) >= 0 ? 'text-emerald-300' : 'text-rose-300'}
        />
        <StatTile
          icon={Percent}
          label="Win rate"
          value={`${((aggregate?.win_rate ?? 0) * 100).toFixed(1)}%`}
          hint={`${counts?.wins ?? 0}W / ${counts?.losses ?? 0}L / ${counts?.flat ?? 0} flat`}
        />
        <StatTile
          icon={TrendingUp}
          label="Avg R-multiple"
          value={(aggregate?.avg_r_multiple ?? 0).toFixed(2)}
          hint={`total ${(aggregate?.total_r_multiple ?? 0).toFixed(2)} R across the journal`}
        />
        <StatTile
          icon={BookMarked}
          label="Avg cost drag"
          value={`${(aggregate?.avg_cost_bps ?? 0).toFixed(2)} bps`}
          hint="statutory + fee floor per silo"
        />
      </div>

      <ActionPanel
        title="Record a trade"
        description="Gross P&L is (exit − entry) × quantity × direction. The silo's cost floor is then deducted to produce net P&L, net bps and the R-multiple against the stop."
      >
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <div className="space-y-1">
            <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">Silo</Label>
            <Select value={silo} onValueChange={setSilo}>
              <SelectTrigger className="h-8 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {SILOS.map((id) => (
                  <SelectItem key={id} value={id} className="font-mono text-xs">
                    {id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="log-symbol" className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Symbol
            </Label>
            <Input
              id="log-symbol"
              className="h-8 font-mono"
              value={symbol}
              onChange={(event) => setSymbol(event.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">Side</Label>
            <Select value={side} onValueChange={(value) => setSide(value as 'LONG' | 'SHORT')}>
              <SelectTrigger className="h-8 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="LONG" className="font-mono text-xs">
                  LONG
                </SelectItem>
                <SelectItem value="SHORT" className="font-mono text-xs">
                  SHORT
                </SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Setup strategy
            </Label>
            <Select value={setup} onValueChange={setSetup}>
              <SelectTrigger className="h-8 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {SETUPS.map((id) => (
                  <SelectItem key={id} value={id} className="font-mono text-xs">
                    {id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="log-entry" className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Entry
            </Label>
            <Input
              id="log-entry"
              className="h-8 font-mono"
              inputMode="decimal"
              value={entryPrice}
              onChange={(event) => setEntryPrice(event.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="log-exit" className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Exit
            </Label>
            <Input
              id="log-exit"
              className="h-8 font-mono"
              inputMode="decimal"
              value={exitPrice}
              onChange={(event) => setExitPrice(event.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="log-qty" className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Quantity
            </Label>
            <Input
              id="log-qty"
              className="h-8 font-mono"
              inputMode="decimal"
              value={quantity}
              onChange={(event) => setQuantity(event.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="log-stop" className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Stop loss
            </Label>
            <Input
              id="log-stop"
              className="h-8 font-mono"
              inputMode="decimal"
              value={stopLoss}
              onChange={(event) => setStopLoss(event.target.value)}
            />
          </div>
          <div className="space-y-1 sm:col-span-2">
            <Label htmlFor="log-thesis" className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Thesis
            </Label>
            <Input
              id="log-thesis"
              className="h-8"
              value={thesis}
              onChange={(event) => setThesis(event.target.value)}
            />
          </div>
          <div className="space-y-1 sm:col-span-2">
            <Label htmlFor="log-lessons" className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Lessons learned
            </Label>
            <Input
              id="log-lessons"
              className="h-8"
              value={lessons}
              onChange={(event) => setLessons(event.target.value)}
            />
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Select value={workspace} onValueChange={(value) => setWorkspace(value as 'PAPER' | 'LIVE')}>
            <SelectTrigger className="h-8 w-28 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="PAPER" className="font-mono text-xs">
                PAPER
              </SelectItem>
              <SelectItem value="LIVE" className="font-mono text-xs">
                LIVE
              </SelectItem>
            </SelectContent>
          </Select>
          <Button type="button" size="sm" onClick={record} disabled={entryAction.pending}>
            <ClipboardList className="mr-1 size-3.5" />
            Record trade
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={clearJournal}
            disabled={clearAction.pending}
          >
            <RotateCcw className="mr-1 size-3.5" />
            Clear journal
          </Button>
        </div>

        <div className="mt-3">
          <ActionFeedback pending={entryAction.pending} error={entryAction.error}>
            {entryAction.receipt ? (
              <div className="grid gap-2 rounded-lg border border-border/70 bg-background/40 px-3 py-2 text-[10px] sm:grid-cols-2 xl:grid-cols-4">
                <ProofRow label="Entry id" value={entryAction.receipt.entry.entry_id} />
                <ProofRow
                  label="Gross"
                  value={formatUsd(entryAction.receipt.entry.gross_pnl, 2)}
                />
                <ProofRow
                  label="Cost drag"
                  value={`${formatUsd(entryAction.receipt.entry.cost_drag, 2)} · ${entryAction.receipt.entry.cost_bps.toFixed(2)} bps`}
                />
                <ProofRow label="Net" value={formatUsd(entryAction.receipt.entry.net_pnl, 2)} />
                <ProofRow
                  label="R-multiple"
                  value={entryAction.receipt.entry.r_multiple.toFixed(2)}
                />
                <ProofRow label="Outcome" value={entryAction.receipt.entry.outcome} />
                <ProofRow label="Workspace" value={entryAction.receipt.entry.workspace} />
                <ProofRow
                  label="Net bps"
                  value={`${entryAction.receipt.entry.net_bps.toFixed(2)}`}
                />
              </div>
            ) : null}
          </ActionFeedback>
        </div>
      </ActionPanel>

      <DataState loading={loading} error={error}>
        <Card className="border-border/70 bg-card/60 shadow-none">
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              <ClipboardList className="size-4 text-sky-300" />
              Execution journal
            </CardTitle>
          </CardHeader>
          <CardContent className="px-0">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[1000px] text-[11px]">
                <thead className="border-y border-border/60 text-[9px] uppercase tracking-wider text-muted-foreground">
                  <tr>
                    <th className="px-3 py-1.5 text-left">Id</th>
                    <th className="px-3 py-1.5 text-left">Silo / symbol</th>
                    <th className="px-3 py-1.5 text-left">Side</th>
                    <th className="px-3 py-1.5 text-left">Setup</th>
                    <th className="px-3 py-1.5 text-right">Entry</th>
                    <th className="px-3 py-1.5 text-right">Exit</th>
                    <th className="px-3 py-1.5 text-right">Qty</th>
                    <th className="px-3 py-1.5 text-right">Gross</th>
                    <th className="px-3 py-1.5 text-right">Cost</th>
                    <th className="px-3 py-1.5 text-right">Net</th>
                    <th className="px-3 py-1.5 text-right">R</th>
                    <th className="px-3 py-1.5 text-left">Outcome</th>
                    <th className="px-3 py-1.5 text-left">When</th>
                  </tr>
                </thead>
                <tbody className="nios-tabular font-mono">
                  {entries.map((row) => (
                    <tr key={row.entry_id} className="border-b border-border/30">
                      <td className="px-3 py-1 text-left text-muted-foreground">{row.entry_id}</td>
                      <td className="px-3 py-1 text-left">
                        {row.silo} · {row.symbol}
                      </td>
                      <td className="px-3 py-1 text-left">{row.side}</td>
                      <td className="px-3 py-1 text-left text-muted-foreground">
                        {row.setup_strategy}
                      </td>
                      <td className="px-3 py-1 text-right">{formatNumber(row.entry_price, 4)}</td>
                      <td className="px-3 py-1 text-right">{formatNumber(row.exit_price, 4)}</td>
                      <td className="px-3 py-1 text-right">{formatNumber(row.quantity, 2)}</td>
                      <td className="px-3 py-1 text-right">{formatNumber(row.gross_pnl)}</td>
                      <td className="px-3 py-1 text-right text-amber-300/80">
                        {formatNumber(row.cost_drag)}
                      </td>
                      <td
                        className={
                          row.net_pnl >= 0
                            ? 'px-3 py-1 text-right text-emerald-300'
                            : 'px-3 py-1 text-right text-rose-300'
                        }
                      >
                        {formatNumber(row.net_pnl)}
                      </td>
                      <td className="px-3 py-1 text-right">{row.r_multiple.toFixed(2)}</td>
                      <td className="px-3 py-1 text-left">
                        <span
                          className={
                            row.outcome === 'WIN'
                              ? 'inline-flex items-center gap-1 text-emerald-300'
                              : row.outcome === 'LOSS'
                                ? 'inline-flex items-center gap-1 text-rose-300'
                                : 'text-muted-foreground'
                          }
                        >
                          {row.outcome === 'WIN' ? (
                            <TrendingUp className="size-3" />
                          ) : row.outcome === 'LOSS' ? (
                            <TrendingDown className="size-3" />
                          ) : null}
                          {row.outcome}
                        </span>
                      </td>
                      <td className="px-3 py-1 text-left text-muted-foreground">
                        {formatClock(row.recorded_at_ns)}
                      </td>
                    </tr>
                  ))}
                  {entries.length === 0 ? (
                    <tr>
                      <td colSpan={13} className="px-3 py-8 text-center text-muted-foreground">
                        Clean slate — no trades recorded. The journal is empty by design until you
                        record a round trip.
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>
            <div className="flex flex-wrap items-center gap-3 px-3 pt-2 text-[10px] text-muted-foreground">
              {Object.entries(data?.cost_floor_bps ?? {}).map(([id, bps]) => (
                <span key={id} className="font-mono">
                  {id} {bps.toFixed(2)} bps
                </span>
              ))}
            </div>
          </CardContent>
        </Card>
      </DataState>
    </PageShell>
  )
}
