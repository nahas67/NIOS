import { Boxes, Calculator, Cpu, RefreshCw, ShieldHalf } from 'lucide-react'
import { useState } from 'react'

import { ActionFeedback, ActionPanel, ProofRow } from '@/components/operator-action'
import { PageShell, StatTile, DataState } from '@/components/page-shell'
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
import { fetchQuantPage, postPriceOption, postQuantScan } from '@/lib/api'
import { formatUsd } from '@/lib/format'
import { cell, num, rowKey } from '@/lib/rows'
import type {
  ActivationEntryInfo,
  InteractiveQuantScan,
  OptionPriceReceipt,
} from '@/types'

export function QuantPage() {
  const { data, error, loading } = usePageData(fetchQuantPage)
  const scan = data?.universe_scan
  const candidates = scan?.candidates ?? []
  const datasets = data?.datasets
  const valuation = Object.entries(data?.valuation ?? {})
  const hedger = Object.entries(data?.tail_hedger ?? {})

  const [optionSymbol, setOptionSymbol] = useState('BTC-OPT')
  const [spot, setSpot] = useState('50000')
  const [strike, setStrike] = useState('48000')
  const [volatility, setVolatility] = useState('0.65')
  const [daysToExpiry, setDaysToExpiry] = useState('30')
  const [riskFreeRate, setRiskFreeRate] = useState('0.05')
  const [optionType, setOptionType] = useState<'PUT' | 'CALL'>('PUT')

  const pricer = useAction<OptionPriceReceipt>()
  const rescan = useAction<InteractiveQuantScan>()
  const liveScan = rescan.receipt
  const rows = liveScan?.candidates ?? candidates

  async function priceOption() {
    const payload = {
      symbol: optionSymbol.trim().toUpperCase() || 'BTC-OPT',
      spot: Number(spot),
      strike: Number(strike),
      volatility: Number(volatility),
      days_to_expiry: Number(daysToExpiry),
      risk_free_rate: Number(riskFreeRate),
      option_type: optionType,
    }
    if (
      !Number.isFinite(payload.spot) ||
      !Number.isFinite(payload.strike) ||
      !Number.isFinite(payload.volatility) ||
      !Number.isFinite(payload.days_to_expiry) ||
      payload.spot <= 0 ||
      payload.strike <= 0
    ) {
      return
    }
    await pricer.run(() => postPriceOption(payload))
  }

  async function rescanFactors() {
    await rescan.run(() => postQuantScan({ top_k: 15 }))
  }

  return (
    <PageShell
      title="Quant Scanner, Valuation Lab & Feeds"
      ring="RING 2"
      capability="PAPER CONTROL"
      subtitle="490-factor vectorized scan, DCF and Black-Scholes valuation, tail-hedger Greeks and the donor loader/engine readiness matrix."
      actions={
        <Badge variant="outline" className="font-mono text-[10px]">
          {scan?.factor_count ?? data?.factors?.count ?? 0} FACTORS
        </Badge>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          icon={Cpu}
          label="Universe scanned"
          value={`${scan?.universe_size ?? 0}`}
          hint={`${scan?.kernel_count ?? 0} sparse kernels · ${(scan?.elapsed_ms ?? 0).toFixed(2)} ms`}
        />
        <StatTile
          icon={Boxes}
          label="Top candidates"
          value={`${candidates.length}`}
          hint={scan?.available === false ? 'Scanner unavailable' : 'Ranked by composite alpha score'}
        />
        <StatTile
          icon={Calculator}
          label="Loaders active"
          value={`${datasets?.loader_module_count ?? 0}`}
          hint={`${datasets?.loader_file_count ?? 0} loader modules discovered`}
        />
        <StatTile
          icon={ShieldHalf}
          label="Regional engines"
          value={`${datasets?.engine_module_count ?? 0}`}
          hint={`${datasets?.import_errors ?? 0} import errors across the donor pack`}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <ActionPanel
          title="Black-Scholes grounded option pricer"
          description="Every input and Greek is sealed into a SHA-256 GroundedFactPacket and re-verified numerically before the receipt is returned."
        >
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <Label
                htmlFor="option-symbol"
                className="text-[10px] uppercase tracking-wider text-muted-foreground"
              >
                Symbol
              </Label>
              <Input
                id="option-symbol"
                value={optionSymbol}
                onChange={(event) => setOptionSymbol(event.target.value.toUpperCase())}
                className="h-8 font-mono text-xs"
              />
            </div>
            <div className="space-y-1">
              <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">
                Option type
              </Label>
              <Select
                value={optionType}
                onValueChange={(value) => setOptionType(value as 'PUT' | 'CALL')}
              >
                <SelectTrigger className="h-8 w-full text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="PUT" className="text-xs">
                    PUT
                  </SelectItem>
                  <SelectItem value="CALL" className="text-xs">
                    CALL
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label
                htmlFor="option-spot"
                className="text-[10px] uppercase tracking-wider text-muted-foreground"
              >
                Spot
              </Label>
              <Input
                id="option-spot"
                type="number"
                min="0"
                step="0.01"
                value={spot}
                onChange={(event) => setSpot(event.target.value)}
                className="h-8 font-mono text-xs"
              />
            </div>
            <div className="space-y-1">
              <Label
                htmlFor="option-strike"
                className="text-[10px] uppercase tracking-wider text-muted-foreground"
              >
                Strike
              </Label>
              <Input
                id="option-strike"
                type="number"
                min="0"
                step="0.01"
                value={strike}
                onChange={(event) => setStrike(event.target.value)}
                className="h-8 font-mono text-xs"
              />
            </div>
            <div className="space-y-1">
              <Label
                htmlFor="option-vol"
                className="text-[10px] uppercase tracking-wider text-muted-foreground"
              >
                Volatility
              </Label>
              <Input
                id="option-vol"
                type="number"
                min="0.0001"
                step="0.01"
                value={volatility}
                onChange={(event) => setVolatility(event.target.value)}
                className="h-8 font-mono text-xs"
              />
            </div>
            <div className="space-y-1">
              <Label
                htmlFor="option-dte"
                className="text-[10px] uppercase tracking-wider text-muted-foreground"
              >
                Days to expiry
              </Label>
              <Input
                id="option-dte"
                type="number"
                min="1"
                step="1"
                value={daysToExpiry}
                onChange={(event) => setDaysToExpiry(event.target.value)}
                className="h-8 font-mono text-xs"
              />
            </div>
            <div className="space-y-1">
              <Label
                htmlFor="option-rate"
                className="text-[10px] uppercase tracking-wider text-muted-foreground"
              >
                Risk-free rate
              </Label>
              <Input
                id="option-rate"
                type="number"
                step="0.005"
                value={riskFreeRate}
                onChange={(event) => setRiskFreeRate(event.target.value)}
                className="h-8 font-mono text-xs"
              />
            </div>
            <div className="flex items-end">
              <Button
                type="button"
                size="sm"
                className="w-full"
                disabled={pricer.pending}
                onClick={priceOption}
              >
                <Calculator className="mr-1 size-3.5" />
                Price &amp; Ground
              </Button>
            </div>
          </div>

          <div className="mt-3">
            <ActionFeedback pending={pricer.pending} error={pricer.error}>
              {pricer.receipt ? <OptionProof receipt={pricer.receipt} /> : null}
            </ActionFeedback>
          </div>
        </ActionPanel>

        <ActionPanel
          title="Vectorized 490-factor scan"
          description="Re-runs the sparse-kernel scan across the full factor zoo and returns the freshly ranked candidates with the measured latency."
        >
          <div className="flex flex-wrap items-end gap-3">
            <Button
              type="button"
              size="sm"
              disabled={rescan.pending}
              onClick={rescanFactors}
            >
              <RefreshCw className="mr-1 size-3.5" />
              Re-Scan 490 Factors
            </Button>
            <span className="font-mono text-[10px] text-muted-foreground">
              {liveScan
                ? `${liveScan.factor_count} factors · ${liveScan.universe_size} symbols · ${liveScan.scan_latency_ms.toFixed(2)} ms`
                : 'Idle — last page hydration only'}
            </span>
          </div>

          <div className="mt-3">
            <ActionFeedback pending={rescan.pending} error={rescan.error}>
              {liveScan ? (
                <div className="rounded-lg border border-border/70 bg-background/40 p-3">
                  <div className="mb-2 flex flex-wrap items-center gap-2">
                    <Badge variant="outline" className="font-mono text-[10px]">
                      {liveScan.status}
                    </Badge>
                    <span className="font-mono text-[10px] text-muted-foreground">
                      {liveScan.candidate_count} candidates · {liveScan.kernel_count} kernels ·{' '}
                      {liveScan.compression_ratio.toFixed(2)}x compression
                    </span>
                  </div>
                  <ProofRow label="Scan id" value={liveScan.scan_id} />
                  <ProofRow
                    label="Scan latency"
                    value={`${liveScan.scan_latency_ms.toFixed(3)} ms`}
                  />
                  <ProofRow
                    label="Reported elapsed"
                    value={`${liveScan.reported_elapsed_ms.toFixed(2)} ms`}
                  />
                  <ProofRow label="Top K" value={String(liveScan.top_k)} />
                </div>
              ) : null}
            </ActionFeedback>
          </div>
        </ActionPanel>
      </div>

      <DataState loading={loading} error={error}>
        <Card className="border-border/70 bg-card/60 shadow-none">
          <CardHeader className="px-4">
            <CardTitle className="text-sm">Vectorized factor scan — top candidates</CardTitle>
            <p className="text-[11px] text-muted-foreground">
              Every row carries a SHA-256 commitment over its numeric claims.
              {liveScan ? ` Fresh scan in ${liveScan.scan_latency_ms.toFixed(2)} ms.` : ''}
            </p>
          </CardHeader>
          <CardContent className="px-4">
            {rows.length === 0 ? (
              <p className="rounded-lg border border-dashed border-border/70 px-3 py-6 text-center text-xs text-muted-foreground">
                {scan?.error
                  ? `Scanner unavailable: ${scan.error}`
                  : 'No candidates in the current scan window.'}
              </p>
            ) : (
              <table className="w-full text-left text-[11px]">
                <thead className="text-[10px] uppercase tracking-wider text-muted-foreground">
                  <tr>
                    <th className="py-1 pr-3 font-medium">#</th>
                    <th className="py-1 pr-3 font-medium">Symbol</th>
                    <th className="py-1 pr-3 font-medium">Score</th>
                    <th className="py-1 pr-3 font-medium">Price</th>
                    <th className="py-1 pr-3 font-medium">Features</th>
                    <th className="py-1 font-medium">Digest</th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {rows.map((row) => (
                    <tr key={`${row.rank}-${row.symbol}`} className="border-t border-border/40">
                      <td className="py-1 pr-3 tabular-nums text-muted-foreground">{row.rank}</td>
                      <td className="py-1 pr-3 font-medium">{row.symbol}</td>
                      <td className="py-1 pr-3 tabular-nums">{row.score.toFixed(4)}</td>
                      <td className="py-1 pr-3 tabular-nums">{formatUsd(row.price, 2)}</td>
                      <td className="py-1 pr-3 tabular-nums text-muted-foreground">
                        {row.feature_count ?? '—'}
                      </td>
                      <td className="py-1 text-muted-foreground">
                        {row.sha256_digest ? `${row.sha256_digest.slice(0, 12)}…` : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </CardContent>
        </Card>

        <div className="grid gap-3 lg:grid-cols-2">
          <Card className="border-border/70 bg-card/60 shadow-none">
            <CardHeader className="px-4">
              <CardTitle className="text-sm">Valuation lab & tail hedger</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-1.5 px-4 font-mono text-[11px]">
              {valuation.length === 0 && hedger.length === 0 ? (
                <p className="text-xs text-muted-foreground">No valuation snapshot returned.</p>
              ) : null}
              {valuation.map(([key, value]) => (
                <div key={`val-${key}`} className="flex items-center justify-between gap-2">
                  <span className="text-muted-foreground">{key}</span>
                  <span className="tabular-nums">{cell({ value }, 'value')}</span>
                </div>
              ))}
              {hedger.map(([key, value]) => (
                <div key={`hedge-${key}`} className="flex items-center justify-between gap-2">
                  <span className="text-muted-foreground">hedger.{key}</span>
                  <span className="tabular-nums">{cell({ value }, 'value')}</span>
                </div>
              ))}
            </CardContent>
          </Card>

          <Card className="border-border/70 bg-card/60 shadow-none">
            <CardHeader className="px-4">
              <CardTitle className="text-sm">Donor readiness matrix</CardTitle>
              <p className="text-[11px] text-muted-foreground">
                {datasets?.registry_sources?.length ?? 0} registry sources ·{' '}
                {datasets?.engine_classes?.length ?? 0} engine classes
              </p>
            </CardHeader>
            <CardContent className="space-y-2 px-4">
              <ActivationList label="Loaders" entries={(datasets?.loaders ?? []).slice(0, 8)} />
              <ActivationList label="Engines" entries={(datasets?.engines ?? []).slice(0, 8)} />
            </CardContent>
          </Card>
        </div>
      </DataState>
    </PageShell>
  )
}

function OptionProof({ receipt }: { receipt: OptionPriceReceipt }) {
  const greeks = receipt.greeks
  return (
    <div className="rounded-lg border border-border/70 bg-background/40 p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Badge variant={receipt.grounding.verified ? 'default' : 'destructive'} className="font-mono text-[10px]">
          {receipt.status}
        </Badge>
        <span className="font-mono text-[10px] text-muted-foreground">
          {receipt.symbol} · {receipt.option_type}
        </span>
      </div>
      <div className="grid gap-x-6 sm:grid-cols-2">
        <ProofRow label="Price" value={greeks.price.toFixed(4)} />
        <ProofRow label="Delta" value={greeks.delta.toFixed(4)} />
        <ProofRow label="Gamma" value={greeks.gamma.toFixed(6)} />
        <ProofRow label="Vega" value={greeks.vega.toFixed(4)} />
        <ProofRow label="Theta" value={greeks.theta.toFixed(4)} />
        <ProofRow label="Rho" value={greeks.rho.toFixed(4)} />
      </div>
      <div className="mt-2 space-y-1 border-t border-border/40 pt-2">
        <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
          SHA-256 grounding hash
        </p>
        <p className="break-all font-mono text-[10px]">{receipt.grounding.sha256_digest}</p>
        <p className="font-mono text-[10px] text-muted-foreground">
          {receipt.grounding.claims_checked} claims verified ·{' '}
          {receipt.grounding.derived_expressions} derived expressions · tolerance{' '}
          {receipt.grounding.tolerance}
        </p>
      </div>
    </div>
  )
}

interface ActivationListProps {
  label: string
  entries: ActivationEntryInfo[]
}

function ActivationList({ label, entries }: ActivationListProps) {
  return (
    <div className="space-y-1">
      <p className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
        {label}
      </p>
      {entries.length === 0 ? (
        <p className="text-[11px] text-muted-foreground">No modules reported.</p>
      ) : (
        <div className="flex flex-wrap gap-1">
          {entries.map((entry) => (
            <Badge
              key={`${label}-${entry.module}`}
              variant={entry.ok ? 'outline' : 'destructive'}
              className="font-mono text-[9px]"
              title={entry.error || entry.module}
            >
              {entry.module}
            </Badge>
          ))}
        </div>
      )}
      <p className="font-mono text-[10px] text-muted-foreground">
        {entries.filter((entry) => entry.ok).length} ok ·{' '}
        {entries.filter((entry) => !entry.ok).length} failing
      </p>
    </div>
  )
}

export function activationRowKey(entry: ActivationEntryInfo, index: number): string {
  return rowKey({ module: entry.module, index }, index)
}

export function activationCount(entries: ActivationEntryInfo[]): number {
  return num({ count: entries.length }, 'count')
}
