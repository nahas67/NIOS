import { Boxes, Calculator, Cpu, ShieldHalf } from 'lucide-react'

import { PageShell, StatTile, DataState } from '@/components/page-shell'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { usePageData } from '@/hooks/use-page-data'
import { fetchQuantPage } from '@/lib/api'
import { formatUsd } from '@/lib/format'
import { cell, num, rowKey } from '@/lib/rows'
import type { ActivationEntryInfo } from '@/types'

export function QuantPage() {
  const { data, error, loading } = usePageData(fetchQuantPage)
  const scan = data?.universe_scan
  const candidates = scan?.candidates ?? []
  const datasets = data?.datasets
  const valuation = Object.entries(data?.valuation ?? {})
  const hedger = Object.entries(data?.tail_hedger ?? {})

  return (
    <PageShell
      title="Quant Scanner, Valuation Lab & Feeds"
      ring="RING 2"
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

      <DataState loading={loading} error={error}>
        <Card className="border-border/70 bg-card/60 shadow-none">
          <CardHeader className="px-4">
            <CardTitle className="text-sm">Vectorized factor scan — top candidates</CardTitle>
            <p className="text-[11px] text-muted-foreground">
              Every row carries a SHA-256 commitment over its numeric claims.
            </p>
          </CardHeader>
          <CardContent className="px-4">
            {candidates.length === 0 ? (
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
                  {candidates.map((row) => (
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
