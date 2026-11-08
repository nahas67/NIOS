import { AlertTriangle, Radio, Sparkles } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { formatClock } from '@/lib/format'
import type { CockpitCard } from '@/types'

interface CockpitCardViewProps {
  card: CockpitCard
}

export function CockpitCardView({ card }: CockpitCardViewProps) {
  const isHalt = card.mode_badge.includes('HALT')
  const isShadow = card.mode_badge.includes('SHADOW')
  const Icon = isHalt ? AlertTriangle : isShadow ? Radio : Sparkles

  return (
    <article
      className={`rounded-lg border px-3 py-2.5 ${
        isHalt
          ? 'border-red-500/30 bg-red-500/8'
          : isShadow
            ? 'border-border/70 bg-muted/30'
            : 'border-emerald-500/25 bg-emerald-500/5'
      }`}
    >
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Icon
            className={`size-3.5 ${
              isHalt ? 'text-red-300' : isShadow ? 'text-muted-foreground' : 'text-emerald-300'
            }`}
          />
          <Badge
            variant={isHalt ? 'destructive' : isShadow ? 'secondary' : 'default'}
            className="text-[9px] uppercase tracking-wider"
          >
            {card.mode_badge}
          </Badge>
          <span className="font-mono text-[10px] text-muted-foreground">
            {card.silo} · {card.symbol}
          </span>
        </div>
        <span className="font-mono text-[10px] tabular-nums text-muted-foreground">
          {formatClock(card.timestamp_ns)}
        </span>
      </div>
      <p className="text-[12px] leading-relaxed text-foreground/90">{card.story_sentence}</p>
      <p className="mt-1.5 break-words font-mono text-[10px] leading-relaxed text-muted-foreground">
        {card.proof_line}
      </p>
    </article>
  )
}
