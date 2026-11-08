import { Landmark, ShieldCheck, TrendingUp, Zap } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { formatBps, formatCompactUsd, microsToUsd } from '@/lib/format'
import type { SiloState, VaultKey } from '@/types'

const VAULT_META: Record<
  VaultKey,
  { label: string; hint: string; icon: typeof Landmark; accent: string }
> = {
  ReserveYield: {
    label: 'Reserve Yield',
    hint: '15% ratchet',
    icon: ShieldCheck,
    accent: 'text-sky-300',
  },
  LongTermCompounder: {
    label: 'Long-Term Compounder',
    hint: '35% ratchet',
    icon: TrendingUp,
    accent: 'text-emerald-300',
  },
  TacticalSwing: {
    label: 'Tactical Swing',
    hint: 'discretionary',
    icon: Landmark,
    accent: 'text-amber-300',
  },
  SpinalScalper: {
    label: 'Spinal Scalper',
    hint: '50% retained',
    icon: Zap,
    accent: 'text-fuchsia-300',
  },
}

const VAULT_ORDER: VaultKey[] = [
  'ReserveYield',
  'LongTermCompounder',
  'TacticalSwing',
  'SpinalScalper',
]

interface VaultCardProps {
  silo: SiloState
}

export function VaultCard({ silo }: VaultCardProps) {
  const nav = microsToUsd(silo.vaults.nav_micros)
  const dailyPnl = microsToUsd(silo.realized_pnl_micros)

  return (
    <Card className="gap-4 border-border/70 bg-card/60 py-4 shadow-none">
      <CardHeader className="px-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 text-sm">
              {silo.market}
              <Badge
                variant={silo.mode === 'LIVE' ? 'destructive' : 'secondary'}
                className="text-[9px] uppercase tracking-wider"
              >
                {silo.mode}
              </Badge>
            </CardTitle>
            <div className="mt-1 text-[11px] text-muted-foreground">
              {silo.venue} · {silo.ready_adapters.length} adapters ready
            </div>
          </div>
          <div className="text-right">
            <div className="font-mono text-sm font-semibold tabular-nums">
              {formatCompactUsd(nav)}
            </div>
            <div
              className={`font-mono text-[11px] tabular-nums ${
                dailyPnl >= 0 ? 'text-emerald-300' : 'text-red-300'
              }`}
            >
              {formatBps((dailyPnl / Math.max(nav, 1)) * 10_000)} today
            </div>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3 px-4">
        {VAULT_ORDER.map((key) => {
          const meta = VAULT_META[key]
          const Icon = meta.icon
          const balance = microsToUsd(silo.vaults[key]?.micros ?? 0)
          const share = nav > 0 ? Math.min(100, (balance / nav) * 100) : 0
          return (
            <div key={key} className="space-y-1.5">
              <div className="flex items-center justify-between gap-2 text-[11px]">
                <span className="flex items-center gap-1.5 text-muted-foreground">
                  <Icon className={`size-3.5 ${meta.accent}`} />
                  {meta.label}
                  <span className="rounded bg-muted px-1 py-0.5 text-[9px] uppercase tracking-wider text-muted-foreground">
                    {meta.hint}
                  </span>
                </span>
                <span className="font-mono tabular-nums">{formatCompactUsd(balance)}</span>
              </div>
              <div className="h-1 overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full rounded-full bg-primary/70"
                  style={{ width: `${share}%` }}
                />
              </div>
            </div>
          )
        })}
        {silo.frozen ? (
          <div className="rounded-md border border-red-500/30 bg-red-500/10 px-2 py-1 text-[10px] text-red-200">
            {silo.freeze_reason || 'silo frozen'}
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}
