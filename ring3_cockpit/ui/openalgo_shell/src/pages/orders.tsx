import { Layers, Play, Radio, ShieldAlert, Timer, XCircle } from 'lucide-react'
import { useState } from 'react'

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
import { fetchOrdersPage, postCancelOrders, postPaperOrder } from '@/lib/api'
import { formatUsd, microsToUsd } from '@/lib/format'
import { cell, num, rowKey } from '@/lib/rows'
import type {
  CancelOrdersReceipt,
  OrderSide,
  OrdersPageData,
  PaperOrderReceipt,
  PaperOrderStyle,
} from '@/types'

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

const SILO_IDS = ['CRYPTO_BINANCE', 'INDIA_ZERODHA_DHAN', 'US_IBKR', 'FX_MT5'] as const
const DEFAULT_SYMBOLS: Record<string, string> = {
  CRYPTO_BINANCE: 'BTCUSDT',
  INDIA_ZERODHA_DHAN: 'RELIANCE',
  US_IBKR: 'AAPL',
  FX_MT5: 'EURUSD',
}

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

  const [silo, setSilo] = useState<string>('CRYPTO_BINANCE')
  const [symbol, setSymbol] = useState<string>(DEFAULT_SYMBOLS.CRYPTO_BINANCE)
  const [side, setSide] = useState<OrderSide>('BUY')
  const [quantity, setQuantity] = useState<string>('0.25')
  const [orderStyle, setOrderStyle] = useState<PaperOrderStyle>('MAKER_PEG')
  const [fillImmediately, setFillImmediately] = useState(true)

  const order = useAction<PaperOrderReceipt>()
  const cancel = useAction<CancelOrdersReceipt>()

  function onSiloChange(next: string) {
    setSilo(next)
    setSymbol(DEFAULT_SYMBOLS[next] ?? symbol)
  }

  async function submitPaperOrder() {
    const parsed = Number(quantity)
    if (!Number.isFinite(parsed) || parsed <= 0) {
      return
    }
    const receipt = await order.run(() =>
      postPaperOrder({
        silo,
        symbol,
        side,
        quantity: parsed,
        order_style: orderStyle,
        fill_immediately: fillImmediately,
      })
    )
    if (receipt) page.reload()
  }

  async function cancelOpenOrders() {
    const receipt = await cancel.run(() => postCancelOrders({ order_id: 'ALL' }))
    if (receipt) page.reload()
  }

  return (
    <PageShell
      title="Orders & Positions"
      ring="RING 0"
      capability="PAPER CONTROL"
      subtitle="SmartOMS Maker-Peg and TWAP/Iceberg slicers, the crash-atomic outbox and per-silo reconciliation."
      actions={
        <div className="flex items-center gap-2">
          <Badge variant="outline" className="font-mono text-[10px]">
            OUTBOX · {cell(outbox, 'pending')} PENDING
          </Badge>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={cancel.pending}
            onClick={cancelOpenOrders}
          >
            <XCircle className="mr-1 size-3.5" />
            Cancel Open Orders
          </Button>
        </div>
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

      <ActionPanel
        title="Submit PAPER order"
        description="Grounded order -> crash-atomic outbox -> SmartOMS -> IBOR. Refused with 409 while the kill latch is HALTED or the silo is frozen."
      >
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
          <div className="space-y-1">
            <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Silo
            </Label>
            <Select value={silo} onValueChange={onSiloChange}>
              <SelectTrigger className="h-8 w-full text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {SILO_IDS.map((value) => (
                  <SelectItem key={value} value={value} className="font-mono text-xs">
                    {value}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1">
            <Label
              htmlFor="paper-order-symbol"
              className="text-[10px] uppercase tracking-wider text-muted-foreground"
            >
              Symbol
            </Label>
            <Input
              id="paper-order-symbol"
              value={symbol}
              onChange={(event) => setSymbol(event.target.value.toUpperCase())}
              className="h-8 font-mono text-xs"
            />
          </div>

          <div className="space-y-1">
            <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Side
            </Label>
            <Select value={side} onValueChange={(value) => setSide(value as OrderSide)}>
              <SelectTrigger className="h-8 w-full text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="BUY" className="text-xs">
                  BUY
                </SelectItem>
                <SelectItem value="SELL" className="text-xs">
                  SELL
                </SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1">
            <Label
              htmlFor="paper-order-quantity"
              className="text-[10px] uppercase tracking-wider text-muted-foreground"
            >
              Quantity
            </Label>
            <Input
              id="paper-order-quantity"
              type="number"
              min="0"
              step="0.01"
              value={quantity}
              onChange={(event) => setQuantity(event.target.value)}
              className="h-8 font-mono text-xs"
            />
          </div>

          <div className="space-y-1">
            <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Execution style
            </Label>
            <Select
              value={orderStyle}
              onValueChange={(value) => setOrderStyle(value as PaperOrderStyle)}
            >
              <SelectTrigger className="h-8 w-full text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="MAKER_PEG" className="text-xs">
                  MAKER_PEG
                </SelectItem>
                <SelectItem value="TWAP_ICEBERG" className="text-xs">
                  TWAP_ICEBERG
                </SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="flex items-end gap-2">
            <Button
              type="button"
              size="sm"
              className="w-full"
              disabled={order.pending}
              onClick={submitPaperOrder}
            >
              <Play className="mr-1 size-3.5" />
              Submit Paper Order
            </Button>
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-4">
          <label className="flex items-center gap-2 text-[11px] text-muted-foreground">
            <input
              type="checkbox"
              checked={fillImmediately}
              onChange={(event) => setFillImmediately(event.target.checked)}
              className="size-3.5 accent-primary"
            />
            Fill immediately (uncheck to leave a working order)
          </label>
          <span className="font-mono text-[10px] text-muted-foreground">
            PAPER MODE ONLY · in-silo ratchet 35% / 15% / 50%
          </span>
        </div>

        <div className="mt-3 space-y-2">
          <ActionFeedback pending={order.pending} error={order.error}>
            {order.receipt ? <PaperOrderProof receipt={order.receipt} /> : null}
          </ActionFeedback>
          <ActionFeedback pending={cancel.pending} error={cancel.error}>
            {cancel.receipt ? <CancelProof receipt={cancel.receipt} /> : null}
          </ActionFeedback>
        </div>
      </ActionPanel>

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
                  {Object.entries(positions).map(([siloKey, book]) => (
                    <div
                      key={siloKey}
                      className="rounded-lg border border-border/70 bg-background/40 p-3"
                    >
                      <div className="font-mono text-[11px] font-semibold">{siloKey}</div>
                      <div className="mt-1 flex flex-wrap gap-2 text-[10px] text-muted-foreground">
                        {Object.entries(book as Record<string, number>).map(([ticker, qty]) => (
                          <span
                            key={ticker}
                            className="rounded bg-muted/40 px-1.5 py-0.5 font-mono"
                          >
                            {ticker} · {qty}
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

function PaperOrderProof({ receipt }: { receipt: PaperOrderReceipt }) {
  const split = receipt.ratchet.split_micros
  return (
    <div className="rounded-lg border border-border/70 bg-background/40 p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Badge variant={receipt.filled ? 'default' : 'secondary'} className="font-mono text-[10px]">
          {receipt.status}
        </Badge>
        <span className="font-mono text-[11px]">{receipt.order_id}</span>
        <span className="font-mono text-[10px] text-muted-foreground">
          {receipt.silo} · {receipt.symbol} · {receipt.side} · {receipt.order_style}
        </span>
      </div>
      <div className="grid gap-x-6 sm:grid-cols-2">
        <ProofRow label="Order id" value={receipt.order_id} />
        <ProofRow
          label="Fill price"
          value={receipt.fill_price === null ? 'WORKING' : formatUsd(receipt.fill_price, 2)}
        />
        <ProofRow label="Notional" value={formatUsd(microsToUsd(receipt.notional_micros), 2)} />
        <ProofRow
          label="Realized net"
          value={formatUsd(microsToUsd(receipt.realized_net_micros), 2)}
        />
        <ProofRow label="Ratchet applied" value={receipt.ratchet.applied ? 'YES' : 'NO'} />
        <ProofRow
          label="Split 35/15/50"
          value={`${formatUsd(microsToUsd(split.long_term_micros), 2)} / ${formatUsd(
            microsToUsd(split.reserve_micros),
            2
          )} / ${formatUsd(microsToUsd(split.scalper_micros), 2)}`}
        />
      </div>
    </div>
  )
}

function CancelProof({ receipt }: { receipt: CancelOrdersReceipt }) {
  return (
    <div className="rounded-lg border border-border/70 bg-background/40 p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Badge variant="outline" className="font-mono text-[10px]">
          {receipt.status}
        </Badge>
        <span className="font-mono text-[10px] text-muted-foreground">
          {receipt.cancelled_count} orders · {receipt.voided_outbox_keys.length} outbox keys voided
        </span>
      </div>
      <ProofRow label="Requested" value={receipt.order_id} />
      <ProofRow
        label="Cancelled ids"
        value={receipt.cancelled_order_ids.length === 0 ? 'none' : receipt.cancelled_order_ids.join(', ')}
      />
      <ProofRow
        label="Voided keys"
        value={receipt.voided_outbox_keys.length === 0 ? 'none' : receipt.voided_outbox_keys.join(', ')}
      />
    </div>
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
