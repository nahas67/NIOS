import { BookOpen, Coins, RefreshCw, Scale, Vault } from 'lucide-react'

import { ActionFeedback, ActionPanel, ProofRow } from '@/components/operator-action'
import { PageShell, StatTile, DataState } from '@/components/page-shell'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'
import { useAction } from '@/hooks/use-action'
import { usePageData } from '@/hooks/use-page-data'
import { fetchLedgerPage, postLedgerReconcile } from '@/lib/api'
import { formatCompactUsd, microsToUsd } from '@/lib/format'
import { cell, num, rowKey } from '@/lib/rows'
import type { LedgerReconcileReceipt } from '@/types'

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

  const reconcile = useAction<LedgerReconcileReceipt>()

  async function runSweep() {
    await reconcile.run(() => postLedgerReconcile())
  }

  return (
    <PageShell
      title="IBOR Double-Entry Ledger & Tax Lots"
      ring="RING 0"
      capability="PAPER CONTROL"
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

      <ActionPanel
        title="Run 4-silo IBOR reconciliation sweep"
        description="LiveReconciler compares every internal position against the broker book, freezes any silo above the 100 ppm drift bound, and proves sum(debits) == sum(credits)."
      >
        <div className="flex flex-wrap items-center gap-3">
          <Button type="button" size="sm" disabled={reconcile.pending} onClick={runSweep}>
            <RefreshCw className="mr-1 size-3.5" />
            Run 4-Silo IBOR Reconciliation Sweep
          </Button>
          <span className="font-mono text-[10px] text-muted-foreground">
            threshold {reconcile.receipt?.threshold_ppm ?? 100} ppm · 60 s background reconciler
          </span>
        </div>

        <div className="mt-3">
          <ActionFeedback pending={reconcile.pending} error={reconcile.error}>
            {reconcile.receipt ? <ReconcileProof receipt={reconcile.receipt} /> : null}
          </ActionFeedback>
        </div>
      </ActionPanel>

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

function ReconcileProof({ receipt }: { receipt: LedgerReconcileReceipt }) {
  return (
    <div className="rounded-lg border border-border/70 bg-background/40 p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Badge variant={receipt.status === 'RECONCILED' ? 'default' : 'destructive'} className="font-mono text-[10px]">
          {receipt.status}
        </Badge>
        <Badge
          variant={receipt.double_entry.balanced ? 'default' : 'destructive'}
          className="font-mono text-[10px]"
        >
          {receipt.double_entry.balanced ? 'sum(debits) == sum(credits)' : 'DOUBLE-ENTRY DRIFT'}
        </Badge>
        <span className="font-mono text-[10px] text-muted-foreground">
          {receipt.silo_count} silos · {receipt.all_within_threshold ? 'all within threshold' : 'breach'}
        </span>
      </div>

      <div className="space-y-1">
        {Object.entries(receipt.silos).map(([siloId, silo]) => (
          <div
            key={siloId}
            className="flex flex-wrap items-center justify-between gap-2 border-b border-border/30 py-1 last:border-0"
          >
            <span className="font-mono text-[11px]">{siloId}</span>
            <span className="flex items-center gap-2 font-mono text-[10px] text-muted-foreground">
              <span>{silo.drift_ppm.toFixed(2)} ppm</span>
              <Badge
                variant={silo.frozen ? 'destructive' : 'outline'}
                className="font-mono text-[9px]"
              >
                {silo.frozen ? 'FROZEN' : 'ACTIVE'}
              </Badge>
              <Badge
                variant={silo.within_threshold ? 'secondary' : 'destructive'}
                className="font-mono text-[9px]"
              >
                {silo.within_threshold ? 'WITHIN' : 'BREACH'}
              </Badge>
            </span>
          </div>
        ))}
      </div>

      <div className="mt-2 grid gap-x-6 sm:grid-cols-2">
        <ProofRow
          label="Debits"
          value={formatCompactUsd(microsToUsd(receipt.double_entry.debits_micros))}
        />
        <ProofRow
          label="Credits"
          value={formatCompactUsd(microsToUsd(receipt.double_entry.credits_micros))}
        />
      </div>
      <p className="mt-1 break-all font-mono text-[10px] text-muted-foreground">
        {receipt.double_entry.proof}
      </p>
    </div>
  )
}
