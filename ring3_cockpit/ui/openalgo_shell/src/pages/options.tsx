import { useState } from 'react'
import { Activity, Layers, Percent, Scale, Sigma, Target } from 'lucide-react'

import { ActionFeedback, ActionPanel, ProofRow } from '@/components/operator-action'
import { PageShell, StatTile } from '@/components/page-shell'
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
import { postOptionsChain, postStrategyPayoff } from '@/lib/api'
import { formatPct, truncateDigest } from '@/lib/format'
import type { OptionsChainReceipt, StrategyPayoffReceipt } from '@/types'

const STRATEGY_IDS = [
  'BULL_CALL_SPREAD',
  'BEAR_PUT_SPREAD',
  'SHORT_STRADDLE',
  'LONG_STRANGLE',
  'IRON_CONDOR',
  'PROTECTIVE_PUT',
] as const

function formatNumber(value: number, digits = 2): string {
  if (!Number.isFinite(value)) return '—'
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

/** Compact payoff chart: one polyline over 25 expiry points plus a zero line. */
function PayoffChart({ receipt }: { receipt: StrategyPayoffReceipt }) {
  const points = receipt.payoff_curve
  if (points.length < 2) return null
  const width = 640
  const height = 200
  const pnls = points.map((point) => point.pnl)
  const low = Math.min(...pnls, 0)
  const high = Math.max(...pnls, 0)
  const span = Math.max(1e-9, high - low)
  const x = (index: number) => (index / (points.length - 1)) * width
  const y = (value: number) => height - ((value - low) / span) * height
  const zeroY = y(0)
  const path = points.map((point, index) => `${x(index)},${y(point.pnl)}`).join(' ')

  return (
    <svg
      role="img"
      aria-label={`${receipt.strategy} expiry payoff`}
      viewBox={`0 0 ${width} ${height}`}
      className="mt-3 h-48 w-full rounded-lg border border-border/60 bg-background/40"
      preserveAspectRatio="none"
    >
      <line x1={0} y1={zeroY} x2={width} y2={zeroY} stroke="rgba(148,163,184,0.45)" strokeWidth={1} />
      <polyline points={path} fill="none" stroke="#22d3ee" strokeWidth={2} />
    </svg>
  )
}

export function OptionsPage() {
  const [symbol, setSymbol] = useState('NIFTY')
  const [spot, setSpot] = useState('')
  const [volatility, setVolatility] = useState('')
  const [dte, setDte] = useState('')
  const [strikes, setStrikes] = useState('11')
  const [strategy, setStrategy] = useState<string>('IRON_CONDOR')
  const [widthSteps, setWidthSteps] = useState('1')

  const chainAction = useAction<OptionsChainReceipt>()
  const payoffAction = useAction<StrategyPayoffReceipt>()

  const [chain, setChain] = useState<OptionsChainReceipt | null>(null)
  const [payoff, setPayoff] = useState<StrategyPayoffReceipt | null>(null)

  async function buildChain() {
    const receipt = await chainAction.run(() =>
      postOptionsChain({
        symbol,
        strike_count: Number(strikes) || 11,
        ...(spot ? { spot: Number(spot) } : {}),
        ...(volatility ? { volatility: Number(volatility) } : {}),
        ...(dte ? { days_to_expiry: Number(dte) } : {}),
      }),
    )
    if (receipt) setChain(receipt)
  }

  async function buildPayoff() {
    const receipt = await payoffAction.run(() =>
      postStrategyPayoff({
        symbol,
        strategy,
        width_steps: Number(widthSteps) || 1,
        ...(spot ? { spot: Number(spot) } : {}),
        ...(volatility ? { volatility: Number(volatility) } : {}),
        ...(dte ? { days_to_expiry: Number(dte) } : {}),
      }),
    )
    if (receipt) setPayoff(receipt)
  }

  const active = chain
  const summary = active?.summary
  const rows = active?.chain ?? []

  return (
    <PageShell
      title="Options Chain & Multi-Leg Strategy Lab"
      ring="RING 2"
      capability="READ-ONLY"
      subtitle="Deterministic Black-Scholes chain with full Greeks, Put-Call Ratio and Max Pain across all four silos, plus a six-template payoff builder. Every figure is sealed into a SHA-256 grounding packet before it renders."
      actions={
        <Badge variant="outline" className="font-mono text-[10px]">
          6 STRATEGIES · 6 UNDERLYINGS
        </Badge>
      }
    >
      <ActionPanel
        title="Load a chain"
        description="Strikes are snapped to the venue grid around the at-the-money strike. Leave spot / volatility / expiry blank to use the modelled defaults for the chosen underlying."
      >
        <div className="flex flex-wrap items-end gap-3">
          <div className="space-y-1">
            <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Underlying
            </Label>
            <Select value={symbol} onValueChange={setSymbol}>
              <SelectTrigger className="h-8 w-40 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {['NIFTY', 'BANKNIFTY', 'BTCUSDT', 'ETHUSDT', 'SPY', 'EURUSD'].map((id) => (
                  <SelectItem key={id} value={id} className="font-mono text-xs">
                    {id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="opt-spot" className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Spot
            </Label>
            <Input
              id="opt-spot"
              className="h-8 w-28"
              inputMode="decimal"
              placeholder="default"
              value={spot}
              onChange={(event) => setSpot(event.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="opt-vol" className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Volatility
            </Label>
            <Input
              id="opt-vol"
              className="h-8 w-24"
              inputMode="decimal"
              placeholder="0.15"
              value={volatility}
              onChange={(event) => setVolatility(event.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="opt-dte" className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Days
            </Label>
            <Input
              id="opt-dte"
              className="h-8 w-20"
              inputMode="numeric"
              placeholder="7"
              value={dte}
              onChange={(event) => setDte(event.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="opt-strikes" className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Strikes
            </Label>
            <Input
              id="opt-strikes"
              className="h-8 w-20"
              inputMode="numeric"
              value={strikes}
              onChange={(event) => setStrikes(event.target.value)}
            />
          </div>
          <Button type="button" size="sm" onClick={buildChain} disabled={chainAction.pending}>
            <Activity className="mr-1 size-3.5" />
            Load grounded chain
          </Button>
        </div>
        <div className="mt-3">
          <ActionFeedback pending={chainAction.pending} error={chainAction.error}>
            {active ? (
              <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border/70 bg-background/40 px-3 py-2">
                <Badge variant="outline" className="font-mono text-[10px]">
                  {active.symbol} · {active.silo}
                </Badge>
                <span className="font-mono text-[10px] text-muted-foreground">
                  {active.chain.length} strikes · lot {active.lot_size} · step{' '}
                  {formatNumber(active.strike_step, active.symbol === 'EURUSD' ? 4 : 2)}
                </span>
                <span className="font-mono text-[10px] text-emerald-300">
                  SHA-256 {truncateDigest(active.grounding.sha256_digest)}
                </span>
                <span className="font-mono text-[10px] text-muted-foreground">
                  {active.grounding.claims_checked} claims verified
                </span>
              </div>
            ) : (
              <p className="text-[11px] text-muted-foreground">
                No chain loaded yet. Press “Load grounded chain”.
              </p>
            )}
          </ActionFeedback>
        </div>
      </ActionPanel>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          icon={Percent}
          label="ATM implied vol"
          value={summary ? formatPct(summary.atm_iv, 2) : '—'}
          hint={summary ? `ATM strike ${formatNumber(summary.atm_strike)}` : 'load a chain'}
        />
        <StatTile
          icon={Scale}
          label="Put-Call ratio"
          value={summary ? summary.pcr.toFixed(3) : '—'}
          hint={summary ? summary.pcr_regime : 'open interest weighted'}
        />
        <StatTile
          icon={Target}
          label="Max pain strike"
          value={summary ? formatNumber(summary.max_pain_strike) : '—'}
          hint={
            summary
              ? `pain ${formatNumber(summary.max_pain_value, 0)} at that strike`
              : 'buyer-loss minimising strike'
          }
        />
        <StatTile
          icon={Sigma}
          label="Open interest"
          value={
            summary
              ? `${formatNumber(summary.total_call_oi, 0)} / ${formatNumber(summary.total_put_oi, 0)}`
              : '—'
          }
          hint="call OI / put OI across the loaded strikes"
        />
      </div>

      <Card className="border-border/70 bg-card/60 shadow-none">
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            <Layers className="size-4 text-sky-300" />
            Call &amp; Put chain
          </CardTitle>
        </CardHeader>
        <CardContent className="px-0">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[980px] text-[11px]">
              <thead className="border-y border-border/60 text-[9px] uppercase tracking-wider text-muted-foreground">
                <tr>
                  <th className="px-2 py-1.5 text-right">Call OI</th>
                  <th className="px-2 py-1.5 text-right">Call LTP</th>
                  <th className="px-2 py-1.5 text-right">Δ</th>
                  <th className="px-2 py-1.5 text-right">Γ</th>
                  <th className="px-2 py-1.5 text-right">Θ</th>
                  <th className="px-2 py-1.5 text-right">Vega</th>
                  <th className="px-2 py-1.5 text-center">Strike</th>
                  <th className="px-2 py-1.5 text-left">Put LTP</th>
                  <th className="px-2 py-1.5 text-left">Δ</th>
                  <th className="px-2 py-1.5 text-left">Γ</th>
                  <th className="px-2 py-1.5 text-left">Θ</th>
                  <th className="px-2 py-1.5 text-left">Vega</th>
                  <th className="px-2 py-1.5 text-left">Put OI</th>
                </tr>
              </thead>
              <tbody className="nios-tabular font-mono">
                {rows.map((row) => {
                  const atMoney = summary
                    ? Math.abs(row.strike - summary.atm_strike) < 1e-9
                    : false
                  return (
                    <tr
                      key={row.strike}
                      className={
                        atMoney
                          ? 'nios-atm-row border-b border-border/40'
                          : 'border-b border-border/30 hover:bg-accent/20'
                      }
                    >
                      <td className="px-2 py-1 text-right text-muted-foreground">
                        {formatNumber(row.call_oi, 0)}
                      </td>
                      <td className="px-2 py-1 text-right text-sky-200">
                        {formatNumber(row.call_ltp)}
                      </td>
                      <td className="px-2 py-1 text-right">{row.call_delta.toFixed(3)}</td>
                      <td className="px-2 py-1 text-right">{row.call_gamma.toFixed(5)}</td>
                      <td className="px-2 py-1 text-right text-rose-300/80">
                        {row.call_theta.toFixed(4)}
                      </td>
                      <td className="px-2 py-1 text-right">{row.call_vega.toFixed(4)}</td>
                      <td className="px-2 py-1 text-center font-semibold text-foreground">
                        {formatNumber(row.strike, row.strike < 10 ? 4 : 2)}
                      </td>
                      <td className="px-2 py-1 text-left text-violet-200">
                        {formatNumber(row.put_ltp)}
                      </td>
                      <td className="px-2 py-1 text-left">{row.put_delta.toFixed(3)}</td>
                      <td className="px-2 py-1 text-left">{row.put_gamma.toFixed(5)}</td>
                      <td className="px-2 py-1 text-left text-rose-300/80">
                        {row.put_theta.toFixed(4)}
                      </td>
                      <td className="px-2 py-1 text-left">{row.put_vega.toFixed(4)}</td>
                      <td className="px-2 py-1 text-left text-muted-foreground">
                        {formatNumber(row.put_oi, 0)}
                      </td>
                    </tr>
                  )
                })}
                {rows.length === 0 ? (
                  <tr>
                    <td colSpan={13} className="px-3 py-6 text-center text-muted-foreground">
                      Load a chain to render the Call/Put ladder.
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      <ActionPanel
        title="Multi-leg strategy payoff"
        description="Six templates: bull call spread, bear put spread, short straddle, long strangle, iron condor and protective put. The payoff curve samples 25 expiry spots and the packet seals net premium, max profit, max loss and every breakeven."
      >
        <div className="flex flex-wrap items-end gap-3">
          <div className="space-y-1">
            <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Template
            </Label>
            <Select value={strategy} onValueChange={setStrategy}>
              <SelectTrigger className="h-8 w-52 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {STRATEGY_IDS.map((id) => (
                  <SelectItem key={id} value={id} className="font-mono text-xs">
                    {id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="opt-width" className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Wing width (steps)
            </Label>
            <Input
              id="opt-width"
              className="h-8 w-24"
              inputMode="numeric"
              value={widthSteps}
              onChange={(event) => setWidthSteps(event.target.value)}
            />
          </div>
          <Button type="button" size="sm" onClick={buildPayoff} disabled={payoffAction.pending}>
            <Scale className="mr-1 size-3.5" />
            Price strategy
          </Button>
        </div>

        <div className="mt-3">
          <ActionFeedback pending={payoffAction.pending} error={payoffAction.error}>
            {payoff ? (
              <div className="space-y-3">
                <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                  <div className="rounded-lg border border-border/60 bg-background/40 px-3 py-2">
                    <p className="text-[9px] uppercase tracking-wider text-muted-foreground">
                      Net position
                    </p>
                    <p className="nios-tabular font-mono text-sm">
                      {payoff.net_position} {formatNumber(payoff.net_debit_credit)}
                    </p>
                  </div>
                  <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/5 px-3 py-2">
                    <p className="text-[9px] uppercase tracking-wider text-muted-foreground">
                      Max profit
                    </p>
                    <p className="nios-tabular font-mono text-sm text-emerald-300">
                      {formatNumber(payoff.max_profit)}
                    </p>
                  </div>
                  <div className="rounded-lg border border-rose-500/40 bg-rose-500/5 px-3 py-2">
                    <p className="text-[9px] uppercase tracking-wider text-muted-foreground">
                      Max loss
                    </p>
                    <p className="nios-tabular font-mono text-sm text-rose-300">
                      {formatNumber(payoff.max_loss)}
                      {payoff.unbounded_downside ? ' (sampled)' : ''}
                    </p>
                  </div>
                  <div className="rounded-lg border border-border/60 bg-background/40 px-3 py-2">
                    <p className="text-[9px] uppercase tracking-wider text-muted-foreground">
                      Breakevens
                    </p>
                    <p className="nios-tabular font-mono text-sm">
                      {payoff.breakeven_points.length
                        ? payoff.breakeven_points.map((value) => formatNumber(value)).join(' / ')
                        : 'none in range'}
                    </p>
                  </div>
                </div>

                <div className="overflow-x-auto">
                  <table className="w-full min-w-[720px] text-[11px]">
                    <thead className="border-y border-border/60 text-[9px] uppercase tracking-wider text-muted-foreground">
                      <tr>
                        <th className="px-2 py-1.5 text-left">Leg</th>
                        <th className="px-2 py-1.5 text-right">Strike</th>
                        <th className="px-2 py-1.5 text-right">IV</th>
                        <th className="px-2 py-1.5 text-right">Premium</th>
                        <th className="px-2 py-1.5 text-right">Δ</th>
                        <th className="px-2 py-1.5 text-right">Γ</th>
                        <th className="px-2 py-1.5 text-right">Θ</th>
                        <th className="px-2 py-1.5 text-right">Vega</th>
                      </tr>
                    </thead>
                    <tbody className="nios-tabular font-mono">
                      {payoff.legs.map((leg) => (
                        <tr
                          key={`${leg.leg}-${leg.strike}`}
                          className="border-b border-border/30"
                        >
                          <td className="px-2 py-1 text-left">{leg.leg}</td>
                          <td className="px-2 py-1 text-right">
                            {leg.kind === 'STOCK' ? 'underlying' : formatNumber(leg.strike)}
                          </td>
                          <td className="px-2 py-1 text-right">{leg.iv.toFixed(4)}</td>
                          <td className="px-2 py-1 text-right">{formatNumber(leg.premium)}</td>
                          <td className="px-2 py-1 text-right">{leg.delta.toFixed(3)}</td>
                          <td className="px-2 py-1 text-right">{leg.gamma.toFixed(5)}</td>
                          <td className="px-2 py-1 text-right">{leg.theta.toFixed(4)}</td>
                          <td className="px-2 py-1 text-right">{leg.vega.toFixed(4)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                <PayoffChart receipt={payoff} />

                <div className="flex flex-wrap items-center gap-3 text-[10px] text-muted-foreground">
                  <ProofRow label="Strategy" value={payoff.strategy} />
                  <ProofRow label="Spots sampled" value={`${payoff.curve_points}`} />
                  <ProofRow label="Symbol" value={payoff.symbol} />
                  <ProofRow
                    label="SHA-256"
                    value={truncateDigest(payoff.grounding.sha256_digest)}
                  />
                  <ProofRow
                    label="Claims verified"
                    value={`${payoff.grounding.claims_checked}`}
                  />
                </div>
              </div>
            ) : (
              <p className="text-[11px] text-muted-foreground">
                Pick a template and press “Price strategy” to render its expiry payoff.
              </p>
            )}
          </ActionFeedback>
        </div>
      </ActionPanel>
    </PageShell>
  )
}
