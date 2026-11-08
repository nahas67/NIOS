import { Inbox, Layers, ShieldCheck, Waves } from 'lucide-react'

import { CockpitCardView } from '@/components/cockpit-card-view'
import { VaultCard } from '@/components/vault-card'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { ScrollArea } from '@/components/ui/scroll-area'
import { formatCompactUsd, formatPct, microsToUsd } from '@/lib/format'
import type { CockpitCard, CockpitState } from '@/types'

interface LiveCockpitTabProps {
  state: CockpitState | null
  cards: CockpitCard[]
}

export function LiveCockpitTab({ state, cards }: LiveCockpitTabProps) {
  const silos = state ? Object.values(state.silos) : []
  const totalNav = silos.reduce(
    (sum, silo) => sum + microsToUsd(silo.vaults.nav_micros),
    0
  )
  const abstention = state?.abstention

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1.55fr)_minmax(360px,1fr)]">
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-3">
          <SummaryTile
            icon={Layers}
            label="Total IBOR NAV"
            value={formatCompactUsd(totalNav)}
            hint="4 sovereign silos · zero cross-border wires"
          />
          <SummaryTile
            icon={Waves}
            label="Noise abstention"
            value={formatPct(abstention?.rate ?? 0)}
            hint={`${abstention?.abstentions ?? 0} abstentions / ${abstention?.ticks ?? 0} ticks`}
          />
          <SummaryTile
            icon={ShieldCheck}
            label="Shadow graduation"
            value={`${abstention?.shadow_fills ?? 0} / 30`}
            hint={`${abstention?.trades ?? 0} live proposals · ${abstention?.breaker_trips ?? 0} breaker trips`}
          />
        </div>

        <div className="grid gap-3 lg:grid-cols-2">
          {silos.map((silo) => (
            <VaultCard key={silo.silo_id} silo={silo} />
          ))}
          {silos.length === 0 ? (
            <Card className="col-span-full border-dashed bg-transparent py-10 shadow-none">
              <CardContent className="text-center text-sm text-muted-foreground">
                Waiting for the Ring 3 gateway snapshot…
              </CardContent>
            </Card>
          ) : null}
        </div>
      </div>

      <Card className="gap-3 border-border/70 bg-card/60 py-4 shadow-none">
        <CardHeader className="px-4">
          <div className="flex items-center justify-between gap-2">
            <CardTitle className="text-sm">Plain-English cockpit feed</CardTitle>
            <Badge variant="outline" className="font-mono text-[9px]">
              SSE · 60 FPS
            </Badge>
          </div>
          <p className="text-[11px] text-muted-foreground">
            Every trade and abstention carries its deterministic proof line.
          </p>
        </CardHeader>
        <CardContent className="px-4">
          <ScrollArea className="h-[560px] pr-3">
            {cards.length === 0 ? (
              <div className="flex h-40 flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border/70 text-muted-foreground">
                <Inbox className="size-5" />
                <span className="text-xs">
                  No cockpit cards yet. Ring 1 emits one per shadow trial, fill or breaker
                  trip.
                </span>
              </div>
            ) : (
              <div className="space-y-2">
                {cards.map((card) => (
                  <CockpitCardView
                    key={`${card.timestamp_ns}-${card.silo}-${card.symbol}-${card.mode_badge}`}
                    card={card}
                  />
                ))}
              </div>
            )}
          </ScrollArea>
        </CardContent>
      </Card>
    </div>
  )
}

interface SummaryTileProps {
  icon: typeof Layers
  label: string
  value: string
  hint: string
}

function SummaryTile({ icon: Icon, label, value, hint }: SummaryTileProps) {
  return (
    <div className="rounded-xl border border-border/70 bg-card/60 px-3 py-2.5">
      <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
        <Icon className="size-3.5" />
        {label}
      </div>
      <div className="mt-1 font-mono text-lg font-semibold tabular-nums">{value}</div>
      <div className="text-[10px] text-muted-foreground">{hint}</div>
    </div>
  )
}
