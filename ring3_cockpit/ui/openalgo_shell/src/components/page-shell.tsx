import type { LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'

import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'

interface PageShellProps {
  title: string
  ring: string
  subtitle: string
  actions?: ReactNode
  /**
   * Capability badge. Interactive paper-only pages declare `PAPER CONTROL`;
   * they still cannot sign a live order from Ring 3.
   */
  capability?: 'READ-ONLY' | 'PAPER CONTROL'
  children: ReactNode
}

/** Consistent page header used by every cockpit page. */
export function PageShell({
  title,
  ring,
  subtitle,
  actions,
  capability = 'READ-ONLY',
  children,
}: PageShellProps) {
  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
            <Badge variant="outline" className="font-mono text-[9px]">
              {ring}
            </Badge>
            <Badge
              variant="outline"
              className={
                capability === 'READ-ONLY'
                  ? 'border-emerald-500/40 font-mono text-[9px] text-emerald-300'
                  : 'border-amber-500/40 font-mono text-[9px] text-amber-300'
              }
            >
              {capability}
            </Badge>
          </div>
          <p className="mt-1 max-w-3xl text-[11px] text-muted-foreground">{subtitle}</p>
        </div>
        {actions}
      </header>
      {children}
    </div>
  )
}

interface StatTileProps {
  label: string
  value: string
  hint: string
  accent?: string
  icon?: LucideIcon
}

/** Compact numeric tile used across the multi-page cockpit. */
export function StatTile({ label, value, hint, accent, icon: Icon }: StatTileProps) {
  return (
    <Card className="gap-0 border-border/70 bg-card/60 py-3 shadow-none">
      <CardContent className="px-3">
        <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
          {Icon ? <Icon className="size-3.5" /> : null}
          {label}
        </div>
        <div className={`mt-1 font-mono text-lg font-semibold tabular-nums ${accent ?? ''}`}>
          {value}
        </div>
        <div className="text-[10px] text-muted-foreground">{hint}</div>
      </CardContent>
    </Card>
  )
}

interface DataStateProps {
  loading: boolean
  error: string | null
  children: ReactNode
}

/** Loading / failure wrapper for gateway-backed panels. */
export function DataState({ loading, error, children }: DataStateProps) {
  if (error) {
    return (
      <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-[11px] text-amber-300">
        Ring 3 gateway: {error}. The panel will retry on the next navigation.
      </div>
    )
  }
  if (loading) {
    return (
      <div className="rounded-lg border border-dashed border-border/70 px-3 py-6 text-center text-[11px] text-muted-foreground">
        Loading grounded telemetry…
      </div>
    )
  }
  return <>{children}</>
}
