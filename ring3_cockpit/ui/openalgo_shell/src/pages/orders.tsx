import { Layers, Radio, ShieldAlert, Timer } from 'lucide-react'

import { DataState, PageShell, StatTile } from '@/components/page-shell'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { usePageData } from '@/hooks/use-page-data'
import { fetchOrdersPage } from '@/lib/api'
import { cell, num, rowKey } from '@/lib/rows'
import type { OrdersPageData } from '@/types'

const WORKING_COLUMNS = ['order_id', 'silo', 'symbol', 'side', 'qty', 'price', 'repegs', 'state']
const RESERVATION_COLUMNS = [
  'outbox_idempotency_key',
  'silo',
  'symbol',
  'side',
  'total_lock_micros',
  'state',
]
const EVENT_COLUMNS = ['event_type', 'silo', 'symbol', 'side', 'qty', 'price', 'state', 'reason']
const SLICER_COLUMNS = ['slicer_id', 'silo', 'symbol', 'side', 'slices_done', 'slices_total', 'state']

export function OrdersPage() {
  const page = usePageData(fetchOrdersPage)
  const data: OrdersPageData | null = page.data
  const working = data?.working_orders ?? []
  const slicers = data?.twap_icebergs ?? []
  const reservations = data?.reservations ?? []
  const events = data?.events ?? []
  const outbox = data?.outbox ?? {}
  const reconciliation = data?.reconciliation ?? {}
  const positions = data?.positions ?? {}

  return (
    <PageShell
      title="Orders & Positions"
      ring="RING 0"
      subtitle="SmartOMS Maker-Peg and TWAP/Iceberg slicers, the crash-atomic outbox and per-silo reconciliation."
      actions={
        <Badge variant="outline" className="font-mono text-[10px]">
          OUTBOX · {cell(outbox, 'pending')} PENDING
        </Badge>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          icon={Layers}
          label="Working orders"
          value={String(working.length)}
          hint={`${cell(data?.oms ?? {}, 'events')} execution events recorded`}
        />
        <StatTile
          icon={Timer}
          label="TWAP / Iceberg"
          value={String(slicers.length)}
          hint="<= 10% of top-of-book per slice"
        />
        <StatTile
          icon={Radio}
          label="Outbox reservations"
          value={String(data?.pending_reservations ?? 0)}
          hint={`${cell(outbox, 'reservations')} total · ${cell(outbox, 'latency_budget_ns')} ns budget`}
        />
        <StatTile
          icon={ShieldAlert}
          label="Reconciliation"
          value={reconciliation.available ? 'TRACKED' : 'IDLE'}
          hint={`${num(reconciliation, 'tracked_positions')} broker positions · 0.01% drift bound`}
        />
      </div>

      <DataState loading={page.loading} error={page.error}>
        <Card className="gap-3 border-border/70 bg-card/60 py-4 shadow-none">
          <CardHeader className="px-4">
            <CardTitle className="text-sm">SmartOMS live order blotter</CardTitle>
          </CardHeader>
          <CardContent className="px-4">
            <TelemetryTable
              columns={WORKING_COLUMNS}
              rows={working}
              empty="No working orders. Maker-Peg submissions appear here the moment Ring 0 stages them."
            />
          </CardContent>
        </Card>

        <Card className="gap-3 border-border/70 bg-card/60 py-4 shadow-none">
          <CardHeader className="px-4">
            <CardTitle className="text-sm">Maker-Peg / TWAP Iceberg slicer table</CardTitle>
          </CardHeader>
          <CardContent className="px-4">
            <TelemetryTable
              columns={SLICER_COLUMNS}
              rows={slicers}
              empty="No active slicers on the book."
            />
          </CardContent>
        </Card>

        <div className="grid gap-4 xl:grid-cols-2">
          <Card className="gap-3 border-border/70 bg-card/60 py-4 shadow-none">
            <CardHeader className="px-4">
              <CardTitle className="text-sm">Crash-atomic outbox log</CardTitle>
            </CardHeader>
            <CardContent className="px-4">
              <TelemetryTable
                columns={RESERVATION_COLUMNS}
                rows={reservations}
                empty="No reservations staged. PENDING / COMMITTED / VOIDED receipts land here."
              />
            </CardContent>
          </Card>

          <Card className="gap-3 border-border/70 bg-card/60 py-4 shadow-none">
            <CardHeader className="px-4">
              <CardTitle className="text-sm">Per-silo reconciler positions</CardTitle>
            </CardHeader>
            <CardContent className="px-4">
              {Object.keys(positions).length === 0 ? (
                <p className="rounded-lg border border-dashed border-border/70 px-3 py-6 text-center text-xs text-muted-foreground">
                  No booked positions yet.
                </p>
              ) : (
                <div className="space-y-2">
                  {Object.entries(positions).map(([silo, book]) => (
                    <div
                      key={silo}
                      className="rounded-lg border border-border/70 bg-background/40 p-3"
                    >
                      <div className="font-mono text-[11px] font-semibold">{silo}</div>
                      <div className="mt-1 flex flex-wrap gap-2 text-[10px] text-muted-foreground">
                        {Object.entries(book as Record<string, number>).map(([symbol, qty]) => (
                          <span key={symbol} className="rounded bg-muted/40 px-1.5 py-0.5 font-mono">
                            {symbol} · {qty}
                          </span>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        <Card className="gap-3 border-border/70 bg-card/60 py-4 shadow-none">
          <CardHeader className="px-4">
            <CardTitle className="text-sm">Execution event tape</CardTitle>
          </CardHeader>
          <CardContent className="px-4">
            <TelemetryTable
              columns={EVENT_COLUMNS}
              rows={events}
              empty="No execution events on the tape."
            />
          </CardContent>
        </Card>
      </DataState>
    </PageShell>
  )
}

interface TelemetryTableProps {
  columns: string[]
  rows: Array<Record<string, unknown>>
  empty: string
}

function TelemetryTable({ columns, rows, empty }: TelemetryTableProps) {
  if (rows.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-border/70 px-3 py-6 text-center text-xs text-muted-foreground">
        {empty}
      </p>
    )
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-[11px]">
        <thead>
          <tr className="border-b border-border/60">
            {columns.map((column) => (
              <th
                key={column}
                className="py-1.5 pr-3 font-medium uppercase tracking-wider text-muted-foreground"
              >
                {column.replace(/_/g, ' ')}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={rowKey(row, index)} className="border-b border-border/30 last:border-0">
              {columns.map((column) => (
                <td key={column} className="py-1.5 pr-3 font-mono tabular-nums">
                  {cell(row, column)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
