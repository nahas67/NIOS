import { Lock, ShieldCheck } from 'lucide-react'

import type { AuthTier, WorkspaceMode } from '@/lib/auth'
import { cn } from '@/lib/utils'

interface WorkspaceSwitcherProps {
  mode: WorkspaceMode
  tier: AuthTier
  onNavigate: (path: string) => void
}

/** Top-bar `[ PAPER SANDBOX ]` vs `[ 🔒 LIVE PRODUCTION ]` switcher. */
export function WorkspaceSwitcher({ mode, tier, onNavigate }: WorkspaceSwitcherProps) {
  const liveUnlocked = tier === 'LIVE'
  return (
    <div
      role="tablist"
      aria-label="Workspace"
      className="flex items-center gap-1 rounded-full border border-border/60 bg-card/60 p-1"
    >
      <button
        type="button"
        role="tab"
        aria-selected={mode === 'paper'}
        onClick={() => onNavigate('/paper/dashboard')}
        className={cn(
          'flex items-center gap-1.5 rounded-full px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.14em] transition',
          mode === 'paper'
            ? 'bg-cyan-500/20 text-cyan-200 shadow-[0_0_12px_rgba(6,182,212,0.35)]'
            : 'text-muted-foreground hover:text-foreground',
        )}
      >
        <ShieldCheck className="size-3" />
        Paper Sandbox
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={mode === 'live'}
        onClick={() =>
          onNavigate(liveUnlocked ? '/live/dashboard' : '/live-unlock')
        }
        className={cn(
          'flex items-center gap-1.5 rounded-full px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.14em] transition',
          mode === 'live'
            ? 'bg-rose-500/20 text-rose-200 shadow-[0_0_12px_rgba(244,63,94,0.35)]'
            : 'text-muted-foreground hover:text-foreground',
        )}
      >
        <Lock className="size-3" />
        Live Production
      </button>
    </div>
  )
}

/** Prominent mode banner: cyan/emerald for Paper, crimson/amber for Live. */
export function WorkspaceBanner({
  mode,
  cleanSlate,
  navUnits,
  shadowFills,
  autopilot,
  tier,
}: {
  mode: WorkspaceMode
  cleanSlate?: boolean
  navUnits?: number
  shadowFills?: number
  autopilot?: boolean
  tier?: AuthTier
}) {
  if (mode === 'live') {
    return (
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-rose-500/40 bg-gradient-to-r from-rose-950/60 via-rose-900/30 to-amber-900/20 px-4 py-2 text-[11px] text-rose-100 lg:px-6">
        <span className="flex items-center gap-2 font-semibold uppercase tracking-[0.18em]">
          <Lock className="size-3.5" />
          Live Production — Authenticated
        </span>
        <span className="flex items-center gap-3 text-rose-200/80">
          <span>tier {tier ?? 'NONE'}</span>
          <span className="text-rose-300/60">·</span>
          <span>no paper orders · no paper balances</span>
        </span>
      </div>
    )
  }
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-cyan-500/40 bg-gradient-to-r from-cyan-950/60 via-emerald-950/40 to-teal-900/20 px-4 py-2 text-[11px] text-cyan-100 lg:px-6">
      <span className="flex items-center gap-2 font-semibold uppercase tracking-[0.18em]">
        <ShieldCheck className="size-3.5" />
        Paper Sandbox
        {cleanSlate ? (
          <span className="rounded-full border border-emerald-400/50 bg-emerald-500/10 px-2 py-0.5 text-[9px] text-emerald-200">
            clean slate · all values 0
          </span>
        ) : null}
      </span>
      <span className="flex items-center gap-3 text-cyan-200/90">
        <span>NAV {(navUnits ?? 0).toLocaleString(undefined, { maximumFractionDigits: 2 })}</span>
        <span className="text-cyan-300/50">·</span>
        <span>shadow {shadowFills ?? 0}/30</span>
        <span className="text-cyan-300/50">·</span>
        <span>{autopilot ? 'autopilot ON' : 'autopilot OFF'}</span>
      </span>
    </div>
  )
}
