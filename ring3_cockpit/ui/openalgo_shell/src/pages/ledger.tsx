import { BookOpen, Coins, Scale, Vault } from 'lucide-react'

import { PageShell, StatTile, DataState } from '@/components/page-shell'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'
import { usePageData } from '@/hooks/use-page-data'
import { fetchLedgerPage } from '@/lib/api'
import { formatCompactUsd, microsToUsd } from '@/lib/format'
import { cell, num, rowKey } from '@/lib/rows'

const VAULT_ORDER = [
  'ReserveYield',
  'LongTermCompounder',
  'TacticalSwing',
  'SpinalScalper',
] as const

const TRANSFER_COLUMNS = [
  'transfer_id',
  'silo',
  'debit_account',
  'credit_account',
  'amount_micros',
  'phase',
] as const

export function LedgerPage() {
  const { data, error, loading } = usePageData(fetchLedgerPage)
  const silos = Object.entries(data?.silos ?? {})
  const transfers = data?.transfers ?? []
  const doubleEntry = data?.double_entry
  const ratchet = data?.ratchet
  const backends = Object.entries(data?.backends ?? {})

  return (
    <PageShell
      title="IBOR Double-Entry Ledger & Tax Lots"
      ring="RING 0"
      subtitle="Four silos × four vaults in 128-bit micro-units. Debits always equal credits; profits compound inside the silo."
      actions={
        <Badge
          variant={doubleEntry?.balanced ? 'default' : 'destructive'}
          className="font-mono text-[10px]"
        >
          {doubleEntry?.balanced ? 'BALANCED' : 'DRIFT'}
        </Badge>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          icon={Vault}
          label="Total IBOR NAV"
          value={formatCompactUsd(microsToUsd(num(data?.counts ?? {}, 'nav_micros')))}
          hint={`${data?.counts?.silos ?? silos.length} silos · ${data?.counts?.vaults ?? 16} vaults`}
        />
        <StatTile
          icon={Scale}
          label="Debits vs credits"
          value={doubleEntry?.balanced ? 'IN BALANCE' : 'OUT OF BALANCE'}
          hint={`${formatCompactUsd(microsToUsd(doubleEntry?.debits_micros ?? 0))} posted both sides`}
        />
        <StatTile
          icon={Coins}
          label="In-silo ratchet"
          value={`${ratchet?.long_term_pct ?? 35}% / ${ratchet?.reserve_pct ?? 15}%`}
          hint={`Compounder / reserve · ${ratchet?.cross_border_wire_bps ?? 0} bps cross-border wires`}
        />
        <StatTile
          icon={BookOpen}
          label="Journal transfers"
          value={`${transfers.length}`}
          hint="Append-only double-entry rows"
        />
      </div>

      <DataState loading={loading} error={error}>
        <div className="grid gap-3 lg:grid-cols-2">
          {silos.map(([siloId, silo]) => {
            const vaults = (silo.vaults ?? {}) as Record<string, unknown>
            const navMicros = Number(vaults.nav_micros ?? 0)
            const ratchetPreview = (silo.ratchet_preview ?? {}) as Record<string, unknown>
            const split = (ratchetPreview.split_micros ?? {}) as Record<string, number>
            const floor = (silo.cost_floor ?? {}) as Record<string, unknown>
            return (
              <Card key={siloId} className="border-border/70 bg-card/60 shadow-none">
                <CardHeader className="px-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <CardTitle className="text-sm">{siloId}</CardTitle>
                    <Badge variant="outline" className="font-mono text-[9px]">
                      {formatCompactUsd(microsToUsd(navMicros))}
                    </Badge>
                  </div>
                  <p className="text-[11px] text-muted-foreground">
                    {String(ratchetPreview.long_term_sweep_asset ?? '—')} ·{' '}
                    {String(ratchetPreview.reserve_yield_asset ?? '—')} · tax floor{' '}
                    {cell(floor, 'total_bps')} bps
                  </p>
                </CardHeader>
                <CardContent className="space-y-1.5 px-4">
                  {VAULT_ORDER.map((vault) => {
                    const balance = vaults[vault] as Record<string, number> | undefined
                    const balanceMicros = Number(balance?.micros ?? 0)
                    const value = Number(balance?.nav ?? microsToUsd(balanceMicros))
                    const share =
                      navMicros > 0 ? Math.round((balanceMicros / navMicros) * 100) : 0
                    return (
                      <div key={`${siloId}-${vault}`} className="space-y-1">
                        <div className="flex items-center justify-between gap-2 font-mono text-[11px]">
                          <span className="text-muted-foreground">{vault}</span>
                          <span className="tabular-nums">{formatCompactUsd(value)}</span>
                        </div>
                        <Progress value={share} />
                      </div>
                    )
                  })}
                  <div className="flex flex-wrap gap-1.5 pt-1">
                    {Object.entries(split).map(([key, micros]) => (
                      <Badge
                        key={`${siloId}-split-${key}`}
                        variant="secondary"
                        className="font-mono text-[9px]"
                      >
                        {key} {microsToUsd(micros).toFixed(2)}
                      </Badge>
                    ))}
                  </div>
                </CardContent>
              </Card>
            )
          })}
        </div>

        <Card className="border-border/70 bg-card/60 shadow-none">
          <CardHeader className="px-4">
            <CardTitle className="text-sm">Double-entry journal</CardTitle>
            <p className="text-[11px] text-muted-foreground">
              {formatCompactUsd(microsToUsd(doubleEntry?.debits_micros ?? 0))} debits ·{' '}
              {formatCompactUsd(microsToUsd(doubleEntry?.credits_micros ?? 0))} credits
            </p>
          </CardHeader>
          <CardContent className="px-4">
            {transfers.length === 0 ? (
              <p className="rounded-lg border border-dashed border-border/70 px-3 py-6 text-center text-xs text-muted-foreground">
                No journal transfers recorded yet.
              </p>
            ) : (
              <table className="w-full text-left text-[11px]">
                <thead className="text-[10px] uppercase tracking-wider text-muted-foreground">
                  <tr>
                    {TRANSFER_COLUMNS.map((column) => (
                      <th key={column} className="py-1 pr-3 font-medium">
                        {column.replace(/_/g, ' ')}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {transfers.slice(0, 40).map((row, index) => (
                    <tr key={rowKey(row, index)} className="border-t border-border/40">
                      {TRANSFER_COLUMNS.map((column) => (
                        <td key={column} className="py-1 pr-3 tabular-nums">
                          {cell(row, column)}
                        </td>
                      ))}
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
              <CardTitle className="text-sm">TigerBeetle / PostgreSQL backends</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-1.5 px-4 font-mono text-[11px]">
              {backends.length === 0 ? (
                <p className="text-xs text-muted-foreground">No backend status reported.</p>
              ) : (
                backends.map(([name, value]) => {
                  const status = (value ?? {}) as Record<string, unknown>
                  return (
                    <div key={name} className="flex items-center justify-between gap-2">
                      <span className="text-muted-foreground">{name}</span>
                      <span className="tabular-nums">
                        {String(status.version ?? '—')} ·{' '}
                        {status.available ? 'AVAILABLE' : 'OFFLINE'}
                      </span>
                    </div>
                  )
                })
              )}
            </CardContent>
          </Card>

          <Card className="border-border/70 bg-card/60 shadow-none">
            <CardHeader className="px-4">
              <CardTitle className="text-sm">Ledger invariant report</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-1.5 px-4 font-mono text-[11px]">
              {Object.entries(data?.invariant ?? {}).map(([key, value]) => (
                <div key={key} className="flex items-center justify-between gap-2">
                  <span className="text-muted-foreground">{key}</span>
                  <span className="tabular-nums">{cell({ value }, 'value')}</span>
                </div>
              ))}
            </CardContent>
          </Card>
        </div>
      </DataState>
    </PageShell>
  )
}
