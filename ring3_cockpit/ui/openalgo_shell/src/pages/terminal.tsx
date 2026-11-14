import { useState } from 'react'
import { Activity, ArrowDownRight, ArrowUpRight, BarChart3, Gauge, Waves } from 'lucide-react'

import { ActionFeedback, ActionPanel, ProofRow } from '@/components/operator-action'
import { DataState, PageShell, StatTile } from '@/components/page-shell'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
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
import { fetchTerminalPage, postTerminalQuote } from '@/lib/api'
import { formatBps, truncateDigest } from '@/lib/format'
import type { TerminalFrame } from '@/types'

const SYMBOLS = ['BTCUSDT', 'NIFTY', 'SPY', 'EURUSD'] as const

function formatNumber(value: number, digits = 2): string {
  if (!Number.isFinite(value)) return '—'
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

/** One SVG price panel: close line, MA20 and VWAP, no external chart library. */
function PriceChart({ frame }: { frame: TerminalFrame }) {
  const closes = frame.series.close
  const ma20 = frame.series.ma20
  const vwap = frame.series.vwap
  if (closes.length < 2) return null

  const width = 720
  const height = 220
  const all = [...closes, ...ma20, ...vwap]
  const low = Math.min(...all)
  const high = Math.max(...all)
  const span = Math.max(1e-9, high - low)
  const x = (index: number) => (index / Math.max(1, closes.length - 1)) * width
  const y = (value: number) => height - ((value - low) / span) * height
  const toPath = (series: number[]) =>
    series.map((value, index) => `${x(index)},${y(value)}`).join(' ')

  return (
    <svg
      role="img"
      aria-label={`${frame.symbol} 60-bar close with MA20 and VWAP`}
      viewBox={`0 0 ${width} ${height}`}
      className="h-56 w-full rounded-lg border border-border/60 bg-background/40"
      preserveAspectRatio="none"
    >
      <polyline points={toPath(closes)} fill="none" stroke="#38bdf8" strokeWidth={1.8} />
      <polyline points={toPath(ma20)} fill="none" stroke="#a78bfa" strokeWidth={1.4} />
      <polyline points={toPath(vwap)} fill="none" stroke="#f59e0b" strokeWidth={1.2} strokeDasharray="4 3" />
    </svg>
  )
}

export function TerminalPage() {
  const { data, error, loading } = usePageData(fetchTerminalPage)
  const [symbol, setSymbol] = useState('BTCUSDT')
  const quote = useAction<TerminalFrame>()
  const [frame, setFrame] = useState<TerminalFrame | null>(null)

  const active = frame ?? data?.frame ?? null

  async function loadQuote() {
    const receipt = await quote.run(() => postTerminalQuote({ symbol, bars: 60, depth: 10 }))
    if (receipt) setFrame(receipt)
  }

  const bids = active?.depth.bids ?? []
  const asks = active?.depth.asks ?? []

  return (
    <PageShell
      title="Market Terminal, L2 Depth & Charting"
      ring="RING 1"
      capability="READ-ONLY"
      subtitle="Multi-silo watchlist, a 60-bar OHLCV frame with MA5/MA10/MA20 and VWAP, a 10-level L2 ladder with an imbalance ratio, and a Lee-Ready classified trade tape. The frame is deterministic and sealed with a SHA-256 digest."
      actions={
        <Badge variant="outline" className="font-mono text-[10px]">
          4 SILOS · 60 BARS · 10 LEVELS
        </Badge>
      }
    >
      <ActionPanel
        title="Re-frame a symbol"
        description="Every figure is derived from a SHA-256 of the symbol and bar index, so the same request always renders the same tape — a test can assert on it, and the digest proves it."
      >
        <div className="flex flex-wrap items-end gap-3">
          <div className="space-y-1">
            <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Symbol
            </Label>
            <Select value={symbol} onValueChange={setSymbol}>
              <SelectTrigger className="h-8 w-36 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {SYMBOLS.map((id) => (
                  <SelectItem key={id} value={id} className="font-mono text-xs">
                    {id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button type="button" size="sm" onClick={loadQuote} disabled={quote.pending}>
            <Activity className="mr-1 size-3.5" />
            Load terminal frame
          </Button>
        </div>
        <div className="mt-3">
          <ActionFeedback pending={quote.pending} error={quote.error}>
            {active ? (
              <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border/70 bg-background/40 px-3 py-2 text-[10px] text-muted-foreground">
                <span className="font-mono text-foreground">
                  {active.symbol} · {active.silo}
                </span>
                <span className="font-mono">{active.candles.length} bars</span>
                <span className="font-mono">{active.depth.level_count} levels</span>
                <span className="font-mono">{active.tape.length} prints</span>
                <span className="font-mono text-emerald-300">
                  SHA-256 {truncateDigest(active.grounding.sha256_digest)}
                </span>
              </div>
            ) : (
              <p className="text-[11px] text-muted-foreground">Load a frame to render the tape.</p>
            )}
          </ActionFeedback>
        </div>
      </ActionPanel>

      <DataState loading={loading} error={error}>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <StatTile
            icon={BarChart3}
            label="Last"
            value={active ? formatNumber(active.quote.last, active.symbol === 'EURUSD' ? 5 : 2) : '—'}
            hint={active ? `${active.asset_class} · ${active.silo}` : 'no frame'}
          />
          <StatTile
            icon={Waves}
            label="Session change"
            value={active ? formatBps(active.quote.session_change_bps) : '—'}
            hint="first bar to last bar"
          />
          <StatTile
            icon={Gauge}
            label="Spread"
            value={active ? `${active.quote.spread_bps.toFixed(2)} bps` : '—'}
            hint="quoted half-spread × 2"
          />
          <StatTile
            icon={Activity}
            label="Book imbalance"
            value={active ? active.depth.imbalance_ratio.toFixed(3) : '—'}
            hint={active ? active.depth.imbalance_regime : 'bid vs ask quantity'}
          />
        </div>

        <Card className="border-border/70 bg-card/60 shadow-none">
          <CardHeader className="pb-2">
            <CardTitle className="flex flex-wrap items-center gap-3 text-sm">
              Chart
              <span className="font-mono text-[10px] font-normal text-sky-300">close</span>
              <span className="font-mono text-[10px] font-normal text-violet-300">MA20</span>
              <span className="font-mono text-[10px] font-normal text-amber-300">VWAP</span>
            </CardTitle>
          </CardHeader>
          <CardContent>
            {active ? <PriceChart frame={active} /> : null}
            <div className="mt-3 grid gap-2 text-[10px] text-muted-foreground sm:grid-cols-3">
              <ProofRow
                label="MA5 / MA10 / MA20"
                value={
                  active
                    ? `${active.series.ma5.at(-1)?.toFixed(2) ?? '—'} / ${active.series.ma10.at(-1)?.toFixed(2) ?? '—'} / ${active.series.ma20.at(-1)?.toFixed(2) ?? '—'}`
                    : '—'
                }
              />
              <ProofRow
                label="VWAP"
                value={active ? (active.series.vwap.at(-1)?.toFixed(2) ?? '—') : '—'}
              />
              <ProofRow
                label="Volume (last bar)"
                value={active ? formatNumber(active.series.volume.at(-1) ?? 0, 0) : '—'}
              />
            </div>
          </CardContent>
        </Card>

        <div className="grid gap-3 lg:grid-cols-2">
          <Card className="border-border/70 bg-card/60 shadow-none">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">L2 depth ladder</CardTitle>
            </CardHeader>
            <CardContent className="px-0">
              <table className="w-full text-[11px]">
                <thead className="border-y border-border/60 text-[9px] uppercase tracking-wider text-muted-foreground">
                  <tr>
                    <th className="px-3 py-1.5 text-left">Bid qty</th>
                    <th className="px-3 py-1.5 text-right">Bid</th>
                    <th className="px-3 py-1.5 text-left">Ask</th>
                    <th className="px-3 py-1.5 text-right">Ask qty</th>
                  </tr>
                </thead>
                <tbody className="nios-tabular font-mono">
                  {/* The gateway always returns the same number of bid and ask levels,
                      so the bid ladder drives the row count and its price is a stable key. */}
                  {bids.map((bid, level) => {
                    const ask = asks[level]
                    return (
                      <tr key={`bid-${bid.price}`} className="border-b border-border/30">
                        <td className="px-3 py-1 text-left text-emerald-300/90">
                          {formatNumber(bid.quantity, 0)}
                        </td>
                        <td className="px-3 py-1 text-right text-emerald-200">
                          {formatNumber(bid.price, 6)}
                        </td>
                        <td className="px-3 py-1 text-left text-rose-200">
                          {ask ? formatNumber(ask.price, 6) : '—'}
                        </td>
                        <td className="px-3 py-1 text-right text-rose-300/90">
                          {ask ? formatNumber(ask.quantity, 0) : '—'}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
              <div className="flex flex-wrap items-center gap-3 px-3 pt-2 text-[10px] text-muted-foreground">
                <span>
                  bid qty {active ? formatNumber(active.depth.bid_quantity_total, 0) : '—'}
                </span>
                <span>
                  ask qty {active ? formatNumber(active.depth.ask_quantity_total, 0) : '—'}
                </span>
                <span className="font-mono text-foreground">
                  imbalance {active ? active.depth.imbalance_ratio.toFixed(3) : '—'}
                </span>
              </div>
            </CardContent>
          </Card>

          <Card className="border-border/70 bg-card/60 shadow-none">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">Lee-Ready trade tape</CardTitle>
            </CardHeader>
            <CardContent className="px-0">
              <div className="max-h-72 overflow-y-auto">
                <table className="w-full text-[11px]">
                  <thead className="sticky top-0 border-y border-border/60 bg-card text-[9px] uppercase tracking-wider text-muted-foreground">
                    <tr>
                      <th className="px-3 py-1.5 text-left">#</th>
                      <th className="px-3 py-1.5 text-right">Price</th>
                      <th className="px-3 py-1.5 text-right">Qty</th>
                      <th className="px-3 py-1.5 text-left">Side</th>
                    </tr>
                  </thead>
                  <tbody className="nios-tabular font-mono">
                    {(active?.tape ?? []).map((print) => (
                      <tr key={print.sequence} className="border-b border-border/30">
                        <td className="px-3 py-1 text-left text-muted-foreground">
                          {print.sequence}
                        </td>
                        <td className="px-3 py-1 text-right">{formatNumber(print.price, 6)}</td>
                        <td className="px-3 py-1 text-right">{formatNumber(print.quantity, 0)}</td>
                        <td className="px-3 py-1 text-left">
                          <span
                            className={
                              print.side === 'BUY_INITIATED'
                                ? 'inline-flex items-center gap-1 text-emerald-300'
                                : 'inline-flex items-center gap-1 text-rose-300'
                            }
                          >
                            {print.side === 'BUY_INITIATED' ? (
                              <ArrowUpRight className="size-3" />
                            ) : (
                              <ArrowDownRight className="size-3" />
                            )}
                            {print.side}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="px-3 pt-2 text-[10px] text-muted-foreground">
                Buy ratio {active ? (active.tape_summary.buy_ratio * 100).toFixed(1) : '0.0'}% ·{' '}
                {active ? active.tape_summary.prints : 0} prints classified against the mid.
              </p>
            </CardContent>
          </Card>
        </div>

        <Card className="border-border/70 bg-card/60 shadow-none">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Multi-silo watchlist</CardTitle>
          </CardHeader>
          <CardContent className="px-0">
            <table className="w-full text-[11px]">
              <thead className="border-y border-border/60 text-[9px] uppercase tracking-wider text-muted-foreground">
                <tr>
                  <th className="px-3 py-1.5 text-left">Symbol</th>
                  <th className="px-3 py-1.5 text-left">Silo</th>
                  <th className="px-3 py-1.5 text-right">Last</th>
                  <th className="px-3 py-1.5 text-right">Change</th>
                  <th className="px-3 py-1.5 text-right">Bid</th>
                  <th className="px-3 py-1.5 text-right">Ask</th>
                  <th className="px-3 py-1.5 text-right">Spread</th>
                </tr>
              </thead>
              <tbody className="nios-tabular font-mono">
                {(active?.quotes ?? []).map((row) => (
                  <tr key={row.symbol} className="border-b border-border/30">
                    <td className="px-3 py-1 text-left">{row.symbol}</td>
                    <td className="px-3 py-1 text-left text-muted-foreground">{row.silo}</td>
                    <td className="px-3 py-1 text-right">{formatNumber(row.last, 4)}</td>
                    <td
                      className={
                        row.change_bps >= 0
                          ? 'px-3 py-1 text-right text-emerald-300'
                          : 'px-3 py-1 text-right text-rose-300'
                      }
                    >
                      {formatBps(row.change_bps)}
                    </td>
                    <td className="px-3 py-1 text-right">{formatNumber(row.bid, 4)}</td>
                    <td className="px-3 py-1 text-right">{formatNumber(row.ask, 4)}</td>
                    <td className="px-3 py-1 text-right">{row.spread_bps.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      </DataState>
    </PageShell>
  )
}
