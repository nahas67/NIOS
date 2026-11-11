import { useState } from 'react'
import {
  Activity,
  CircleDollarSign,
  Radio,
  Satellite,
  ShieldAlert,
  ShieldCheck,
  Skull,
  Zap,
} from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import { Switch } from '@/components/ui/switch'
import { postFeedMode } from '@/lib/api'
import { formatPct, formatUsd, timeAgo } from '@/lib/format'
import type { CockpitState, FeedMode, KillReport, RearmReport } from '@/types'

interface TopBarProps {
  state: CockpitState | null
  connected: boolean
  killReport: KillReport | null
  rearmReport: RearmReport | null
  lastFrameAt: number
  onKill: () => void
  onRearm: () => void
  onFeedChanged?: () => void | Promise<void>
}

export function TopBar({
  state,
  connected,
  killReport,
  rearmReport,
  lastFrameAt,
  onKill,
  onRearm,
  onFeedChanged,
}: TopBarProps) {
  const status = state?.system.status ?? 'HALTED'
  const armed = status === 'ARMED'
  const abstention = state?.abstention
  const llm = state?.llm
  const spend = llm?.daily_spend_usd ?? 0
  const cap = llm?.daily_spend_cap_usd ?? 3.1
  const spendPct = cap > 0 ? Math.min(100, (spend / cap) * 100) : 0

  // ---- Phase 3 Step 2: feed mode hot-swap ----------------------------
  const [feedBusy, setFeedBusy] = useState(false)
  const [feedOverride, setFeedOverride] = useState<FeedMode | null>(null)
  const [feedNotice, setFeedNotice] = useState<string | null>(null)
  const [feedFailed, setFeedFailed] = useState(false)

  const serverMode = (state?.feed?.feed_mode ??
    state?.feed_mode ??
    'PAPER_AUTOPILOT') as FeedMode
  const feedMode: FeedMode = feedOverride ?? serverMode
  const liveFeed = feedMode === 'LIVE_PUBLIC_DUAL_VENDOR'
  const lastCycle = state?.feed?.last_cycle
  const cycleAccepted = lastCycle?.accepted ?? 0
  const cycleFrames = lastCycle?.frames ?? 0

  const toggleFeed = async (checked: boolean) => {
    const next: FeedMode = checked
      ? 'LIVE_PUBLIC_DUAL_VENDOR'
      : 'PAPER_AUTOPILOT'
    setFeedBusy(true)
    setFeedNotice(null)
    setFeedFailed(false)
    try {
      const report = await postFeedMode(next, true)
      setFeedOverride(report.feed_mode)
      const cycle = report.cycle
      setFeedNotice(
        next === 'LIVE_PUBLIC_DUAL_VENDOR'
          ? `Live dual-vendor armed · ${cycle?.accepted ?? 0}/${cycle?.frames ?? 0} frames accepted across ${Object.keys(cycle?.silos ?? {}).length} silos`
          : 'Paper autopilot tape restored · zero restart'
      )
      await onFeedChanged?.()
    } catch (cause) {
      setFeedFailed(true)
      setFeedNotice(
        cause instanceof Error ? cause.message : 'feed hot-swap failed'
      )
    } finally {
      setFeedBusy(false)
    }
  }

  return (
    <header className="sticky top-0 z-40 border-b border-border/80 bg-background/85 backdrop-blur-xl">
      <div className="flex flex-wrap items-center gap-4 px-4 py-3 lg:px-6">
        <div className="flex items-center gap-3">
          <div className="flex size-10 items-center justify-center rounded-xl border border-primary/30 bg-primary/10">
            <Zap className="size-5 text-primary" />
          </div>
          <div>
            <div className="text-[10px] font-semibold uppercase tracking-[0.28em] text-muted-foreground">
              NIOS Core v3.0
            </div>
            <div className="text-sm font-semibold tracking-tight">Glass Cockpit</div>
          </div>
          <Badge
            variant={armed ? 'default' : 'destructive'}
            className={
              armed
                ? 'border-emerald-400/40 bg-emerald-500/15 text-emerald-300'
                : 'animate-pulse border-red-400/50 bg-red-500/20 text-red-200'
            }
          >
            {armed ? <Activity className="size-3" /> : <ShieldAlert className="size-3" />}
            {status}
          </Badge>
        </div>

        <div className="hidden h-9 w-px bg-border lg:block" />

        {/* Feed mode hot-swap (Phase 3 Step 2) */}
        <div
          className={`flex items-center gap-2 rounded-xl border px-3 py-1.5 ${
            liveFeed
              ? 'border-emerald-500/40 bg-emerald-500/10'
              : 'border-border/70 bg-muted/30'
          }`}
          data-testid="feed-mode-toggle"
        >
          <Satellite
            className={`size-4 ${liveFeed ? 'text-emerald-300' : 'text-muted-foreground'}`}
          />
          <div className="leading-tight">
            <div className="text-[9px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
              Feed mode
            </div>
            <div
              className={`font-mono text-[10px] ${
                liveFeed ? 'text-emerald-200' : 'text-foreground'
              }`}
            >
              {liveFeed ? 'LIVE DUAL-VENDOR' : 'PAPER AUTOPILOT'}
            </div>
          </div>
          <Switch
            checked={liveFeed}
            disabled={feedBusy || !state}
            onCheckedChange={(checked) => void toggleFeed(checked)}
            aria-label="Hot-swap between the paper autopilot tape and the live multi-silo dual-vendor public feed"
          />
          <span className="font-mono text-[9px] tabular-nums text-muted-foreground">
            {feedBusy ? '…' : `${cycleAccepted}/${cycleFrames}`}
          </span>
        </div>

        <div className="flex min-w-[190px] flex-1 items-center gap-3">
          <div className="flex size-8 items-center justify-center rounded-lg bg-muted">
            <Radio className="size-4 text-muted-foreground" />
          </div>
          <div className="w-full">
            <div className="mb-1 flex items-baseline justify-between gap-2 text-[11px]">
              <span className="font-medium uppercase tracking-wider text-muted-foreground">
                Noise abstention
              </span>
              <span className="font-mono tabular-nums">
                {formatPct(abstention?.rate ?? 0)}
                <span className="ml-1 text-muted-foreground">
                  / {formatPct(abstention?.target_rate ?? 0.991)}
                </span>
              </span>
            </div>
            <Progress value={Math.min(100, (abstention?.rate ?? 0) * 100)} className="h-1.5" />
          </div>
        </div>

        <div className="flex min-w-[190px] flex-1 items-center gap-3">
          <div className="flex size-8 items-center justify-center rounded-lg bg-muted">
            <CircleDollarSign className="size-4 text-muted-foreground" />
          </div>
          <div className="w-full">
            <div className="mb-1 flex items-baseline justify-between gap-2 text-[11px]">
              <span className="font-medium uppercase tracking-wider text-muted-foreground">
                LLM spend / day
              </span>
              <span className="font-mono tabular-nums">
                {formatUsd(spend, 2)}
                <span className="ml-1 text-muted-foreground">/ {formatUsd(cap, 2)}</span>
              </span>
            </div>
            <Progress
              value={spendPct}
              className={spendPct > 85 ? 'h-1.5 [&>div]:bg-amber-400' : 'h-1.5'}
            />
          </div>
        </div>

        <div className="hidden items-center gap-2 text-[11px] text-muted-foreground xl:flex">
          <span
            className={`size-2 rounded-full ${connected ? 'bg-emerald-400' : 'bg-red-400'}`}
            aria-hidden
          />
          <span className="font-mono tabular-nums">
            {connected ? `SSE 60 FPS · ${timeAgo(lastFrameAt)}` : 'SSE disconnected'}
          </span>
        </div>

        <Button
          type="button"
          onClick={onKill}
          disabled={!armed}
          className="ml-auto h-10 gap-2 bg-red-600 px-4 font-semibold text-white shadow-[0_0_24px_rgba(220,38,38,0.35)] hover:bg-red-500 disabled:cursor-not-allowed disabled:opacity-40"
          aria-label="Global kill switch: disarm all leases and flatten positions in under one millisecond"
        >
          <Skull className="size-4" />
          GLOBAL KILL SWITCH
          <span className="rounded bg-black/25 px-1.5 py-0.5 font-mono text-[10px]">
            &lt;1ms FLATTEN
          </span>
        </Button>

        {armed ? null : (
          <Button
            type="button"
            onClick={onRearm}
            className="h-10 gap-2 border border-emerald-400/40 bg-emerald-600 px-4 font-semibold text-white shadow-[0_0_24px_rgba(16,185,129,0.35)] hover:bg-emerald-500"
            aria-label="Re-arm the system: clear the kill latch, unfreeze every silo and resume the paper autopilot without a restart"
          >
            <ShieldCheck className="size-4" />
            RE-ARM SYSTEM
          </Button>
        )}
      </div>

      {feedNotice ? (
        <div
          data-testid="feed-mode-notice"
          className={`border-t px-4 py-1.5 text-[11px] lg:px-6 ${
            feedFailed
              ? 'border-red-500/30 bg-red-500/10 text-red-200'
              : 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200'
          }`}
        >
          {feedFailed ? <ShieldAlert className="mr-1 inline size-3" /> : null}
          {feedNotice}
        </div>
      ) : null}

      {rearmReport ? (
        <div className="border-t border-emerald-500/30 bg-emerald-500/10 px-4 py-1.5 text-[11px] text-emerald-200 lg:px-6">
          Re-armed by <span className="font-mono">{rearmReport.operator}</span> ·{' '}
          {rearmReport.rearmed_silos.length} silos ACTIVE · kill latch{' '}
          {rearmReport.kill_latch_hot ? 'HOT' : 'clear'} · autopilot{' '}
          {rearmReport.autopilot_resumes ? 'resumed' : 'idle'} · no restart required
        </div>
      ) : null}

      {killReport ? (
        <div className="border-t border-red-500/30 bg-red-500/10 px-4 py-1.5 text-[11px] text-red-200 lg:px-6">
          Kill switch fired in{' '}
          <span className="font-mono tabular-nums">
            {(killReport.latency_ns / 1_000_000).toFixed(3)} ms
          </span>{' '}
          · disarmed {killReport.disarmed_leases} leases · cancelled{' '}
          {killReport.cancelled_orders} entries · voided {killReport.voided_reservations}{' '}
          reservations · flattened {killReport.flattened_orders} positions
        </div>
      ) : null}
    </header>
  )
}
