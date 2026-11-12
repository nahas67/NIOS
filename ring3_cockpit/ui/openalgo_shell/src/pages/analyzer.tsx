import { Activity, FlaskConical, GraduationCap, Timer, Zap } from 'lucide-react'
import { useState } from 'react'

import { ActionFeedback, ActionPanel, ProofRow } from '@/components/operator-action'
import { PageShell, StatTile, DataState } from '@/components/page-shell'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Progress } from '@/components/ui/progress'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useAction } from '@/hooks/use-action'
import { usePageData } from '@/hooks/use-page-data'
import { fetchAnalyzerPage, postShadowBurst } from '@/lib/api'
import { formatBps, formatPct } from '@/lib/format'
import { cell } from '@/lib/rows'
import type { ShadowBurstReceipt } from '@/types'

const SILO_IDS = ['CRYPTO_BINANCE', 'INDIA_ZERODHA_DHAN', 'US_IBKR', 'FX_MT5'] as const

export function AnalyzerPage() {
  const { data, error, loading, reload } = usePageData(fetchAnalyzerPage)
  const nursery = data?.nursery
  const strategies = nursery?.strategies ?? []
  const required = nursery?.required_fills ?? 30

  const [silo, setSilo] = useState<string>('CRYPTO_BINANCE')
  const burst = useAction<ShadowBurstReceipt>()

  async function runBurst() {
    const receipt = await burst.run(() => postShadowBurst({ silo, rounds: 10 }))
    if (receipt) reload()
  }

  return (
    <PageShell
      title="Shadow Analyzer & Graduation Nursery"
      ring="RING 1"
      capability="PAPER CONTROL"
      subtitle="OpenAlgo analyzer shadow hooks plus the 30-fill L2 shadow twin. No strategy graduates without adversarial paper evidence."
      actions={
        <Badge variant="outline" className="font-mono text-[10px]">
          {required} FILLS REQUIRED
        </Badge>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          icon={GraduationCap}
          label="Graduated strategies"
          value={`${nursery?.graduated_count ?? 0}`}
          hint={`${strategies.length} tracked in the nursery`}
        />
        <StatTile
          icon={Timer}
          label="Shadow fills"
          value={`${data?.paper?.shadow_fills ?? 0}`}
          hint={`${data?.paper?.ticks ?? 0} paper ticks observed`}
        />
        <StatTile
          icon={Activity}
          label="Live proposals"
          value={`${data?.live?.live_proposals ?? 0}`}
          hint={`${data?.live?.breaker_trips ?? 0} breaker trips · ${data?.live?.ring0_rejects ?? 0} Ring 0 rejects`}
        />
        <StatTile
          icon={FlaskConical}
          label="Q98 residual"
          value={formatBps(strategies[0]?.q98_bps ?? 0)}
          hint={nursery?.calibrated ? 'Conformal window calibrated' : 'Cold start — abstaining'}
        />
      </div>

      <ActionPanel
        title="Run shadow-fill burst"
        description="Drives +10 L2 shadow execution ticks through the twin so the operator can graduate a strategy to 30/30 fills without waiting for the nightly tape."
      >
        <div className="flex flex-wrap items-end gap-3">
          <div className="space-y-1">
            <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Silo
            </Label>
            <Select value={silo} onValueChange={setSilo}>
              <SelectTrigger className="h-8 w-56 text-xs">
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
          <Button type="button" size="sm" disabled={burst.pending} onClick={runBurst}>
            <Zap className="mr-1 size-3.5" />
            Run +10 Shadow Fills Burst
          </Button>
        </div>

        <div className="mt-3">
          <ActionFeedback pending={burst.pending} error={burst.error}>
            {burst.receipt ? (
              <div className="rounded-lg border border-border/70 bg-background/40 p-3">
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <Badge
                    variant={burst.receipt.graduated ? 'default' : 'outline'}
                    className="font-mono text-[10px]"
                  >
                    {burst.receipt.graduated ? 'GRADUATED' : burst.receipt.status}
                  </Badge>
                  <span className="font-mono text-[10px] text-muted-foreground">
                    {burst.receipt.silo} · {burst.receipt.strategy_id}
                  </span>
                </div>
                <div className="grid gap-x-6 sm:grid-cols-2">
                  <ProofRow
                    label="Fills"
                    value={`${burst.receipt.shadow_fills}/${burst.receipt.required_fills}`}
                  />
                  <ProofRow
                    label="Gained this burst"
                    value={`+${burst.receipt.fills_gained} in ${burst.receipt.ticks_executed} ticks`}
                  />
                  <ProofRow
                    label="Remaining"
                    value={`${burst.receipt.remaining_fills} fills`}
                  />
                  <ProofRow
                    label="Adverse selection"
                    value={formatBps(burst.receipt.adverse_selection_bps)}
                  />
                  <ProofRow label="Q98 residual" value={formatBps(burst.receipt.q98_bps)} />
                  <ProofRow
                    label="Threshold met"
                    value={burst.receipt.graduation_threshold_met ? 'YES' : 'NO'}
                  />
                </div>
              </div>
            ) : null}
          </ActionFeedback>
        </div>
      </ActionPanel>

      <DataState loading={loading} error={error}>
        <Card className="border-border/70 bg-card/60 shadow-none">
          <CardHeader className="px-4">
            <CardTitle className="text-sm">30-fill graduation nursery</CardTitle>
            <p className="text-[11px] text-muted-foreground">
              Each strategy must survive 30 L2 shadow fills before any live graduation.
            </p>
          </CardHeader>
          <CardContent className="space-y-3 px-4">
            {strategies.length === 0 ? (
              <p className="rounded-lg border border-dashed border-border/70 px-3 py-6 text-center text-xs text-muted-foreground">
                No shadow trials recorded yet — Ring 1 has not emitted a candidate.
              </p>
            ) : (
              strategies.map((row) => (
                <div key={row.strategy_id} className="space-y-1.5">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-xs font-medium">{row.strategy_id}</span>
                      <Badge
                        variant={row.graduated ? 'default' : 'outline'}
                        className="font-mono text-[9px]"
                      >
                        {row.graduated ? 'GRADUATED' : 'NURSERY'}
                      </Badge>
                    </div>
                    <div className="flex items-center gap-3 font-mono text-[10px] text-muted-foreground">
                      <span>
                        {row.completed_fills}/{row.required_fills} fills
                      </span>
                      <span>adv {formatBps(row.adverse_selection_bps)}</span>
                      <span>Q98 {formatBps(row.q98_bps)}</span>
                    </div>
                  </div>
                  <Progress value={Math.min(100, Math.max(0, row.progress_pct))} />
                  <div className="flex items-center justify-between text-[10px] text-muted-foreground">
                    <span>{formatPct(row.progress_pct / 100)} of the graduation ladder</span>
                    <span>{row.remaining_fills} fills remaining</span>
                  </div>
                </div>
              ))
            )}
          </CardContent>
        </Card>

        <div className="grid gap-3 lg:grid-cols-2">
          <Card className="border-border/70 bg-card/60 shadow-none">
            <CardHeader className="px-4">
              <CardTitle className="text-sm">Paper vs live analyzer stats</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-1.5 px-4 font-mono text-[11px]">
              {Object.entries(data?.paper ?? {}).map(([key, value]) => (
                <div key={key} className="flex items-center justify-between gap-2">
                  <span className="text-muted-foreground">paper.{key}</span>
                  <span className="tabular-nums">{cell({ value }, 'value')}</span>
                </div>
              ))}
              {Object.entries(data?.live ?? {}).map(([key, value]) => (
                <div key={key} className="flex items-center justify-between gap-2">
                  <span className="text-muted-foreground">live.{key}</span>
                  <span className="tabular-nums">{cell({ value }, 'value')}</span>
                </div>
              ))}
            </CardContent>
          </Card>

          <Card className="border-border/70 bg-card/60 shadow-none">
            <CardHeader className="px-4">
              <CardTitle className="text-sm">Harvested analyzer surface</CardTitle>
              <p className="text-[11px] text-muted-foreground">{data?.source?.package ?? '—'}</p>
            </CardHeader>
            <CardContent className="flex flex-wrap gap-1.5 px-4">
              {(data?.source?.components ?? []).map((component) => (
                <Badge key={component} variant="outline" className="font-mono text-[10px]">
                  {component}
                </Badge>
              ))}
            </CardContent>
          </Card>
        </div>
      </DataState>
    </PageShell>
  )
}
