import { AlertTriangle, BrainCircuit, Clock3, Fingerprint, ShieldCheck } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { formatBps, formatPct, truncateDigest } from '@/lib/format'
import type { CockpitState, ContagionState, LeaseRow } from '@/types'

interface BrainXrayTabProps {
  state: CockpitState | null
}

const MODE_LABEL: Record<LeaseRow['mode'], string> = {
  DISARMED: 'DISARMED',
  LONG_ONLY: 'LONG-ONLY',
  SHORT_ONLY: 'SHORT-ONLY',
  MEAN_REV: 'MEAN-REV',
}

export function BrainXrayTab({ state }: BrainXrayTabProps) {
  const leases = state?.leases ?? []
  const contagion = state?.contagion

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
      <Card className="gap-3 border-border/70 bg-card/60 py-4 shadow-none">
        <CardHeader className="px-4">
          <div className="flex items-center justify-between gap-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Clock3 className="size-4 text-primary" />
              System 1 execution leases
            </CardTitle>
            <Badge variant="outline" className="font-mono text-[9px]">
              60 s dead-man TTL
            </Badge>
          </div>
          <p className="text-[11px] text-muted-foreground">
            Ring 2 writes a permission, never an order. Ring 1 reads it in 0.005 ms
            median / 0.008 ms p99.
          </p>
        </CardHeader>
        <CardContent className="px-4">
          <ScrollArea className="h-[430px]">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="text-[10px] uppercase tracking-wider">Symbol</TableHead>
                  <TableHead className="text-[10px] uppercase tracking-wider">Mode</TableHead>
                  <TableHead className="text-right text-[10px] uppercase tracking-wider">
                    Target
                  </TableHead>
                  <TableHead className="text-right text-[10px] uppercase tracking-wider">
                    Stop
                  </TableHead>
                  <TableHead className="w-[34%] text-[10px] uppercase tracking-wider">
                    TTL
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {leases.map((lease) => (
                  <TableRow key={`${lease.symbol}-${lease.expires_at_ns}`}>
                    <TableCell className="font-mono text-xs">{lease.symbol}</TableCell>
                    <TableCell>
                      <Badge
                        variant={
                          lease.mode === 'DISARMED'
                            ? 'secondary'
                            : lease.mode === 'MEAN_REV'
                              ? 'outline'
                              : 'default'
                        }
                        className="text-[9px]"
                      >
                        {MODE_LABEL[lease.mode]}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs tabular-nums">
                      {formatBps(lease.target_bps)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs tabular-nums">
                      {formatBps(lease.stop_bps)}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <Progress
                          value={Math.max(0, Math.min(100, lease.ttl_pct * 100))}
                          className="h-1.5"
                        />
                        <span className="w-12 text-right font-mono text-[10px] tabular-nums text-muted-foreground">
                          {lease.ttl_seconds.toFixed(1)}s
                        </span>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
                {leases.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={5} className="py-8 text-center text-xs text-muted-foreground">
                      No active leases. The dead-man TTL has expired or System 1 is
                      disarmed.
                    </TableCell>
                  </TableRow>
                ) : null}
              </TableBody>
            </Table>
          </ScrollArea>
        </CardContent>
      </Card>

      <div className="space-y-4">
        <ContagionCard contagion={contagion} />
        <Card className="gap-3 border-border/70 bg-card/60 py-4 shadow-none">
          <CardHeader className="px-4">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Fingerprint className="size-4 text-primary" />
              SHA-256 Grounding Ledger
            </CardTitle>
          </CardHeader>
          <CardContent className="flex items-center justify-between gap-3 px-4">
            <div className="text-[11px] text-muted-foreground">
              <div>
                <span className="font-mono text-base font-semibold text-foreground">
                  {contagion?.grounding_ledger.entries ?? 0}
                </span>{' '}
                committed fact packets
              </div>
              <div className="mt-1 font-mono text-[10px]">
                latest: {truncateDigest(contagion?.grounding_ledger.latest_digest ?? null)}
              </div>
            </div>
            <Badge
              variant="outline"
              className={
                contagion?.grounding_ledger.verified
                  ? 'border-emerald-400/40 text-emerald-300'
                  : 'border-red-400/40 text-red-300'
              }
            >
              <ShieldCheck className="size-3" />
              {contagion?.grounding_ledger.verified ? 'VERIFIED' : 'UNVERIFIED'}
            </Badge>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

function ContagionCard({ contagion }: { contagion: ContagionState | undefined }) {
  const fragility = contagion?.cascade_fragility_index ?? 0
  const halt = Boolean(contagion?.requires_ring1_halt)
  const groups = contagion?.archetype_groups ?? []
  const archetypes = contagion?.archetypes ?? []

  return (
    <Card className="gap-3 border-border/70 bg-card/60 py-4 shadow-none">
      <CardHeader className="px-4">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            <BrainCircuit className="size-4 text-primary" />
            MiroFish contagion X-ray
          </CardTitle>
          <Badge variant={halt ? 'destructive' : 'outline'} className="text-[9px]">
            {halt ? 'RING 1 HALT' : 'CASCADE NOMINAL'}
          </Badge>
        </div>
        <p className="text-[11px] text-muted-foreground">
          3-tick cascade across five capital-weighted archetypes. Fragility ≥ 0.75
          revokes every long lease.
        </p>
      </CardHeader>
      <CardContent className="space-y-3 px-4">
        <div className="grid grid-cols-3 gap-2">
          <Metric
            label="Crowd asymmetry"
            value={(contagion?.crowd_asymmetry_score ?? 0).toFixed(2)}
          />
          <Metric
            label="Fragility"
            value={formatPct(fragility)}
            danger={fragility >= 0.75}
          />
          <Metric
            label="Narrative exhaustion"
            value={(contagion?.narrative_exhaustion_divergence ?? 0).toFixed(2)}
          />
        </div>

        <div className="space-y-2">
          <div className="flex items-center justify-between text-[10px] uppercase tracking-wider text-muted-foreground">
            <span>Capital-weighted archetypes</span>
            <span className="font-mono normal-case">{contagion?.archetype_count ?? 5} agents</span>
          </div>
          {groups.map((group) => (
            <div key={group.name} className="space-y-1">
              <div className="flex items-center justify-between text-[11px]">
                <span className="text-muted-foreground">{group.name}</span>
                <span className="font-mono tabular-nums">{formatPct(group.weight, 0)}</span>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full rounded-full bg-primary/70"
                  style={{ width: `${Math.min(100, group.weight * 100)}%` }}
                />
              </div>
            </div>
          ))}
          <div className="flex flex-wrap gap-1.5 pt-1">
            {archetypes.map((archetype) => (
              <span
                key={archetype.name}
                className="rounded border border-border/70 px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground"
              >
                {archetype.name.replaceAll('_', ' ')} {formatPct(archetype.weight, 0)}
              </span>
            ))}
          </div>
        </div>

        {fragility >= 0.75 ? (
          <div className="flex items-center gap-2 rounded-md border border-red-500/40 bg-red-500/10 px-2 py-1.5 text-[10px] text-red-200">
            <AlertTriangle className="size-3.5" />
            Fragility {formatPct(fragility)} ≥ 75%: long leases revoked, Ring 1
            reduced to short-only.
          </div>
        ) : null}

        <div className="grid grid-cols-3 gap-2 border-t border-border/60 pt-2 text-[10px] text-muted-foreground">
          <div>
            latency{' '}
            <span className="font-mono text-foreground">
              {(contagion?.latency_ms ?? 0).toFixed(2)} ms
            </span>
          </div>
          <div>
            revoked{' '}
            <span className="font-mono text-foreground">
              {contagion?.revoked_long_leases ?? 0}
            </span>
          </div>
          <div>
            ticks{' '}
            <span className="font-mono text-foreground">
              {(contagion?.tick_flows ?? []).map((flow) => flow.toFixed(2)).join(' / ') || '—'}
            </span>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

function Metric({
  label,
  value,
  danger = false,
}: {
  label: string
  value: string
  danger?: boolean
}) {
  return (
    <div className="rounded-md border border-border/60 bg-muted/20 px-2 py-1.5">
      <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div
        className={`font-mono text-xs font-semibold tabular-nums ${
          danger ? 'text-red-300' : ''
        }`}
      >
        {value}
      </div>
    </div>
  )
}
