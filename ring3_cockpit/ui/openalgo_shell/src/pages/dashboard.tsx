import { Clock3, Gauge, ServerCog, Workflow } from 'lucide-react'

import { LiveCockpitTab } from '@/components/live-cockpit-tab'
import { DataState, PageShell, StatTile } from '@/components/page-shell'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'
import { usePageData } from '@/hooks/use-page-data'
import { fetchPages } from '@/lib/api'
import { formatPct } from '@/lib/format'
import type { CockpitCard, CockpitState, LeaseRow } from '@/types'

interface DashboardPageProps {
  state: CockpitState | null
  cards: CockpitCard[]
}

export function DashboardPage({ state, cards }: DashboardPageProps) {
  const pages = usePageData(fetchPages)
  const leases: LeaseRow[] = state?.leases ?? []
  const counts = pages.data?.counts ?? {}
  const abstention = state?.abstention
  const processes = state?.ring_processes

  return (
    <PageShell
      title="Sovereign Dashboard"
      ring="RING 3"
      subtitle="Four-silo IBOR vaults, the plain-English proof stream and the 60-second System 1 leases."
      actions={
        <Badge variant="outline" className="font-mono text-[10px]">
          {pages.data?.page_count ?? 9} PAGES · {String(counts.silos ?? 4)} SILOS
        </Badge>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          icon={Gauge}
          label="Noise abstention"
          value={formatPct(abstention?.rate ?? 0)}
          hint={`${abstention?.abstentions ?? 0} abstentions / ${abstention?.ticks ?? 0} ticks`}
        />
        <StatTile
          icon={Workflow}
          label="Shadow graduation"
          value={`${abstention?.shadow_fills ?? 0} / 30`}
          hint={`${abstention?.trades ?? 0} live proposals · ${abstention?.breaker_trips ?? 0} breaker trips`}
        />
        <StatTile
          icon={Clock3}
          label="Active leases"
          value={String(leases.length)}
          hint="60 s TTL · dead-man expiry stops Ring 1 initiating"
        />
        <StatTile
          icon={ServerCog}
          label="Ring processes"
          value={String(processes?.distinct_os_processes ?? 0)}
          hint={`${processes?.available ? 'supervisor available' : 'supervisor idle'} · 4-ring isolation`}
        />
      </div>

      <Card className="gap-3 border-border/70 bg-card/60 py-4 shadow-none">
        <CardHeader className="px-4">
          <div className="flex items-center justify-between gap-2">
            <CardTitle className="text-sm">System 1 lease X-ray</CardTitle>
            <Badge variant="outline" className="font-mono text-[9px]">
              SHM · 0.008 ms p99
            </Badge>
          </div>
        </CardHeader>
        <CardContent className="px-4">
          {leases.length === 0 ? (
            <p className="rounded-lg border border-dashed border-border/70 px-3 py-6 text-center text-xs text-muted-foreground">
              No active leases. Ring 1 will not initiate until System 1 writes a fresh
              lease.
            </p>
          ) : (
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {leases.map((lease) => (
                <div
                  key={`${lease.strategy_id_hash}-${lease.symbol}-${lease.expires_at_ns}`}
                  className="rounded-xl border border-border/70 bg-background/40 p-3"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-xs font-semibold">{lease.symbol}</span>
                    <Badge variant="outline" className="font-mono text-[9px]">
                      {lease.mode}
                    </Badge>
                  </div>
                  <div className="mt-2 flex items-center justify-between text-[10px] text-muted-foreground">
                    <span>target {lease.target_bps.toFixed(1)} bps</span>
                    <span className="font-mono">{lease.ttl_seconds.toFixed(1)} s</span>
                  </div>
                  <Progress value={Math.round(lease.ttl_pct * 100)} className="mt-2 h-1.5" />
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <DataState loading={pages.loading} error={pages.error}>
        <LiveCockpitTab state={state} cards={cards} />
      </DataState>
    </PageShell>
  )
}
