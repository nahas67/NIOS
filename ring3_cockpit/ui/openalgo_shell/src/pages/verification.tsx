import { Cpu, Gauge, ShieldCheck, Zap } from 'lucide-react'

import { PageShell, StatTile } from '@/components/page-shell'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { usePageData } from '@/hooks/use-page-data'
import { fetchMetrics } from '@/lib/api'
import { cell } from '@/lib/rows'
import type { CockpitState } from '@/types'

interface VerificationPageProps {
  state: CockpitState | null
}

export function VerificationPage({ state }: VerificationPageProps) {
  const metrics = usePageData(fetchMetrics)
  const proofs = asRecord(state?.formal_proofs)
  const suites = asRows(proofs.proofs)
  const vopr = asRecord(proofs.vopr)
  const tuning = asRecord(proofs.os_tuning)
  const backendTb = asRecord(proofs.tigerbeetle)
  const backendPg = asRecord(proofs.postgres)

  return (
    <PageShell
      title="Formal Proofs, Chaos & Telemetry"
      ring="RING 0"
      subtitle="TLA+ breadth-first state exploration, the TigerBeetle VOPR fault campaign, SIGKILL chaos recovery and the live Prometheus exporter."
      actions={
        <Badge variant="outline" className="font-mono text-[10px]">
          {suites.length} PROOF SUITES
        </Badge>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          icon={ShieldCheck}
          label="TLA+ states explored"
          value={`${numberish(proofs.total_states ?? proofs.states_explored ?? 0)}`}
          hint={`${suites.length} suites · ${numberish(proofs.violations ?? 0)} violations`}
        />
        <StatTile
          icon={Zap}
          label="VOPR campaign"
          value={`${numberish(vopr.steps ?? vopr.operations ?? 0)}`}
          hint={`${numberish(vopr.faults_injected ?? 0)} faults injected`}
        />
        <StatTile
          icon={Gauge}
          label="Kill switch"
          value={`${numberish(proofs.kill_switch_ns ?? 0)} ns`}
          hint="Budget: < 1,000,000 ns for a global flatten"
        />
        <StatTile
          icon={Cpu}
          label="Backends"
          value={`${backendTb.available ? 'TB' : '—'} / ${backendPg.available ? 'PG' : '—'}`}
          hint={`TigerBeetle ${String(backendTb.version ?? 'n/a')} · PG ${String(backendPg.version ?? 'n/a')}`}
        />
      </div>

      <Card className="border-border/70 bg-card/60 shadow-none">
        <CardHeader className="px-4">
          <CardTitle className="text-sm">TLA+ BFS proof table</CardTitle>
          <p className="text-[11px] text-muted-foreground">
            KillSwitch · OrderLifecycle · Outbox · Reconciliation — every invariant is
            model-checked, never asserted.
          </p>
        </CardHeader>
        <CardContent className="space-y-2 px-4">
          {suites.length === 0 ? (
            <p className="rounded-lg border border-dashed border-border/70 px-3 py-6 text-center text-xs text-muted-foreground">
              No proof suites reported yet.
            </p>
          ) : (
            suites.map((row, index) => (
              <div
                key={String(row.name ?? row.spec ?? `suite-${index}`)}
                className="rounded-lg border border-border/50 bg-background/40 px-3 py-2"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-mono text-xs font-medium">
                    {String(row.name ?? row.spec ?? 'suite')}
                  </span>
                  <Badge
                    variant={row.passed === false ? 'destructive' : 'outline'}
                    className="font-mono text-[9px]"
                  >
                    {row.passed === false ? 'VIOLATED' : 'HOLDS'}
                  </Badge>
                </div>
                <KeyValues data={row} />
              </div>
            ))
          )}
        </CardContent>
      </Card>

      <div className="grid gap-3 lg:grid-cols-2">
        <Card className="border-border/70 bg-card/60 shadow-none">
          <CardHeader className="px-4">
            <CardTitle className="text-sm">VOPR fault campaign & chaos recovery</CardTitle>
          </CardHeader>
          <CardContent className="px-4">
            <KeyValues data={vopr} />
          </CardContent>
        </Card>

        <Card className="border-border/70 bg-card/60 shadow-none">
          <CardHeader className="px-4">
            <CardTitle className="text-sm">OS low-latency tuning</CardTitle>
          </CardHeader>
          <CardContent className="px-4">
            {Object.keys(tuning).length === 0 ? (
              <p className="text-[11px] text-muted-foreground">
                No tuning snapshot exposed by the gateway.
              </p>
            ) : (
              <KeyValues data={tuning} />
            )}
          </CardContent>
        </Card>
      </div>

      <Card className="border-border/70 bg-card/60 shadow-none">
        <CardHeader className="px-4">
          <CardTitle className="text-sm">Prometheus / OpenTelemetry metrics</CardTitle>
          <p className="text-[11px] text-muted-foreground">
            Scrape targets: <span className="font-mono">/metrics</span> and{' '}
            <span className="font-mono">/api/v2/metrics</span>
          </p>
        </CardHeader>
        <CardContent className="px-4">
          {metrics.error ? (
            <p className="text-[11px] text-amber-400">{metrics.error}</p>
          ) : metrics.loading ? (
            <p className="text-[11px] text-muted-foreground">Loading telemetry…</p>
          ) : (
            <KeyValues data={asRecord(metrics.data)} />
          )}
        </CardContent>
      </Card>
    </PageShell>
  )
}

function asRecord(value: unknown): Record<string, unknown> {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }
  return {}
}

function asRows(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return []
  return value.map((row) => asRecord(row))
}

function numberish(value: unknown): number {
  if (typeof value === 'number' && Number.isFinite(value)) return Math.round(value)
  if (typeof value === 'string') {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return Math.round(parsed)
  }
  return 0
}

function KeyValues({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data).filter(
    ([, value]) =>
      value === null ||
      typeof value === 'string' ||
      typeof value === 'number' ||
      typeof value === 'boolean'
  )
  if (entries.length === 0) {
    return <p className="text-[11px] text-muted-foreground">No scalar fields reported.</p>
  }
  return (
    <div className="grid gap-1 font-mono text-[11px]">
      {entries.map(([key, value]) => (
        <div key={key} className="flex items-center justify-between gap-2">
          <span className="text-muted-foreground">{key}</span>
          <span className="tabular-nums">{cell({ value }, 'value')}</span>
        </div>
      ))}
    </div>
  )
}
