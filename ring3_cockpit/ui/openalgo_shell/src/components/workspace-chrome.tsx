import { Lock, ShieldCheck } from 'lucide-react'

import type { AuthTier, WorkspaceMode } from '@/lib/auth'
import { cn } from '@/lib/utils'

interface WorkspaceSwitcherProps {
  mode: WorkspaceMode
  tier: AuthTier
  onNavigate: (path: string) => void
}

/**
 * `[ PAPER SANDBOX ]` vs `[ 🔒 LIVE PRODUCTION ]`.
 *
 * A locked live vault routes to `/live/login` — the dedicated portal — rather
 * than the legacy two-step `/live-unlock`, because V1.3 mints LIVE sessions from
 * one three-factor request.
 */
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
        onClick={() => onNavigate(liveUnlocked ? '/live/dashboard' : '/live/login')}
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

/** One compact, mode-tinted chip used across both shell ribbons. */
export function ShellChip({
  tone,
  children,
  className,
}: {
  tone: 'paper' | 'live' | 'neutral'
  children: React.ReactNode
  className?: string
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-mono text-[9px] uppercase tracking-[0.12em]',
        tone === 'live'
          ? 'nios-live-chip'
          : tone === 'paper'
            ? 'nios-paper-chip'
            : 'border-border/60 bg-card/50 text-muted-foreground',
        className,
      )}
    >
      {children}
    </span>
  )
}

/**
 * Horizontal page strip used inside a shell.
 *
 * The sidebar owns navigation on desktop; this strip keeps every page one tap
 * away in the workspace header and on narrow screens.
 */
export function ShellRouteStrip({
  routes,
  path,
  mode,
  onNavigate,
}: {
  routes: { path: string; label: string }[]
  path: string
  mode: WorkspaceMode
  onNavigate: (path: string) => void
}) {
  return (
    <nav
      aria-label={`${mode} workspace pages`}
      className="flex flex-wrap items-center gap-1"
    >
      {routes.map((entry) => {
        const active = path === entry.path
        return (
          <button
            key={entry.path}
            type="button"
            aria-current={active ? 'page' : undefined}
            onClick={() => onNavigate(entry.path)}
            className={cn(
              'rounded-md px-2.5 py-1 text-[11px] transition',
              active
                ? mode === 'live'
                  ? 'bg-rose-500/20 text-rose-100'
                  : 'bg-cyan-500/20 text-cyan-100'
                : 'text-muted-foreground hover:bg-muted/40 hover:text-foreground',
            )}
          >
            {entry.label}
          </button>
        )
      })}
    </nav>
  )
}
