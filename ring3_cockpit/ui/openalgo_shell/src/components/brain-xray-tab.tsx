import { useState } from 'react'
import {
  AlertTriangle,
  Boxes,
  BrainCircuit,
  CheckCircle2,
  Clock3,
  Cpu,
  Fingerprint,
  GitBranch,
  HeartPulse,
  RefreshCw,
  ShieldCheck,
  ShieldQuestion,
  Waves,
} from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
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
import { fetchFormalProofs, postSupervisor } from '@/lib/api'
import { formatBps, formatPct, truncateDigest } from '@/lib/format'
import type {
  CockpitState,
  ContagionState,
  FeedState,
  FormalProofState,
  LeaseRow,
  RingProcessesState,
  TailHedgerState,
} from '@/types'

interface BrainXrayTabProps {
  state: CockpitState | null
}

const MODE_LABEL: Record<LeaseRow['mode'], string> = {
  DISARMED: 'DISARMED',
  LONG_ONLY: 'LONG-ONLY',
  SHORT_ONLY: 'SHORT-ONLY',
  MEAN_REV: 'MEAN-REV',
}

const DEFAULT_FRAGILITY_TRIGGER = 0.65
const DEFAULT_COPULA_TRIGGER = 0.6

export function BrainXrayTab({ state }: BrainXrayTabProps) {
  const leases = state?.leases ?? []
  const contagion = state?.contagion
  const [proofs, setProofs] = useState<FormalProofState | null>(null)

  // `/api/v2/cockpit/state` already hydrates the cached payload; the dedicated
  // `/api/v2/cockpit/formal-proofs` endpoint lets an operator force a re-run.
  const formal = proofs ?? state?.formal_proofs ?? null

  return (
    <div className="space-y-4">
      <FormalVerificationBanner formal={formal} onRefreshed={setProofs} />

      <RingProcessMatrix rings={state?.ring_processes ?? null} feed={state?.feed ?? null} />

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

      <TailHedgerCard hedger={formal?.tail_hedger ?? null} />
    </div>
  )
}

/* ------------------------------------------------------------------ *
 * Phase 3 Step 2 · 4-ring OS process isolation matrix
 * ------------------------------------------------------------------ */

const RING_ROWS: { worker: string; ring: string; role: string }[] = [
  {
    worker: 'ring0-vault-worker',
    ring: 'Ring 0',
    role: 'Sovereign vault · signer · IBOR · outbox',
  },
  {
    worker: 'ring1-spinal-worker',
    ring: 'Ring 1',
    role: '0.14 ms scalper · breaker · shadow twin',
  },
  {
    worker: 'ring2-brain-worker',
    ring: 'Ring 2',
    role: 'System 1 lease · contagion · factor scan',
  },
  {
    worker: 'ring3-cockpit-gateway',
    ring: 'Ring 3',
    role: 'Kill switch · rearm · control plane',
  },
]

const HEARTBEAT_STALE_MS = 5_000

function RingProcessMatrix({
  rings,
  feed,
}: {
  rings: RingProcessesState | null
  feed: FeedState | null
}) {
  const available = Boolean(rings?.available)
  const byWorker = new Map(
    (rings?.rings ?? []).map((ring) => [ring.worker, ring] as const)
  )
  const feedMode = feed?.feed_mode ?? 'PAPER_AUTOPILOT'
  const liveFeed = feedMode === 'LIVE_PUBLIC_DUAL_VENDOR'
  const siloModes = feed?.silo_modes ?? {}
  const lastCycle = feed?.last_cycle
  const [busy, setBusy] = useState<'start' | 'restart' | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const startSupervisor = async () => {
    setBusy('start')
    setError(null)
    setNote(null)
    try {
      const report = await postSupervisor('start')
      setNote(
        `${report.distinct_os_processes} distinct OS processes · ` +
          Object.entries(report.pids ?? {})
            .map(([worker, pid]) => `${worker.split('-')[0]}:${pid ?? '—'}`)
            .join(' · ')
      )
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(null)
    }
  }

  const restartRing1 = async () => {
    setBusy('restart')
    setError(null)
    setNote(null)
    try {
      const report = await postSupervisor('restart_ring', 1)
      setNote(
        report.changed
          ? `Ring 1 respawned: PID ${report.previous_pid ?? '—'} → ${report.new_pid ?? '—'}`
          : `Ring 1 restart refused: ${String(report.restart?.reason ?? 'unknown')}`
      )
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(null)
    }
  }

  return (
    <Card className="gap-3 border-border/70 bg-card/60 py-4 shadow-none">
      <CardHeader className="px-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            <Boxes className="size-4 text-primary" />
            4-Ring OS Process Isolation Matrix
          </CardTitle>
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              variant={available ? 'secondary' : 'outline'}
              className="font-mono text-[9px] tracking-wider"
            >
              {available
                ? `${rings?.distinct_os_processes ?? 0} DISTINCT OS PROCESSES`
                : 'SINGLE-PROCESS MODE'}
            </Badge>
            <Badge
              variant="outline"
              className={`font-mono text-[9px] ${
                liveFeed
                  ? 'border-emerald-500/40 text-emerald-300'
                  : 'text-muted-foreground'
              }`}
            >
              {liveFeed ? 'LIVE DUAL-VENDOR' : 'PAPER AUTOPILOT'}
            </Badge>
            <Badge variant="outline" className="font-mono text-[9px]">
              SHM SeqLock heartbeat
            </Badge>
            {/* Phase 3 Step 3: start the four OS workers from the cockpit. */}
            <Button
              size="sm"
              variant="outline"
              className="h-6 px-2 font-mono text-[9px]"
              disabled={busy !== null}
              onClick={startSupervisor}
            >
              <Cpu className="mr-1 size-3" />
              {busy === 'start' ? 'STARTING…' : 'START 4 RINGS'}
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-6 px-2 font-mono text-[9px]"
              disabled={busy !== null || !available}
              onClick={restartRing1}
            >
              <RefreshCw
                className={`mr-1 size-3 ${busy === 'restart' ? 'animate-spin' : ''}`}
              />
              RESTART RING 1
            </Button>
          </div>
        </div>
        <p className="text-[11px] text-muted-foreground">
          Four physically separate OS processes share nothing but the lock-free
          shared-memory spine. Ring 2 has no broker key and Ring 1 has no LLM
          client, so the air-gap is enforced by the kernel, not by prompt text.
        </p>
        {note ? (
          <p className="font-mono text-[9px] text-emerald-300">{note}</p>
        ) : null}
        {error ? (
          <p className="font-mono text-[9px] text-amber-300">{error}</p>
        ) : null}
      </CardHeader>
      <CardContent className="space-y-3 px-4">
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          {RING_ROWS.map((row) => {
            const ring = byWorker.get(row.worker)
            const alive = Boolean(ring?.alive)
            const beatAge = ring?.heartbeat_age_ms
            const stale =
              beatAge !== null && beatAge !== undefined && beatAge > HEARTBEAT_STALE_MS
            return (
              <div
                key={row.worker}
                className={`rounded-md border px-3 py-2 ${
                  !available
                    ? 'border-dashed border-border/60 bg-muted/10'
                    : alive && !stale
                      ? 'border-emerald-500/35 bg-emerald-500/[0.05]'
                      : 'border-amber-500/40 bg-amber-500/[0.05]'
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                    <Cpu className="size-3" />
                    {row.ring}
                  </span>
                  <Badge
                    variant={alive && !stale ? 'secondary' : 'outline'}
                    className={`text-[8px] tracking-wider ${
                      !available
                        ? 'text-muted-foreground'
                        : alive && !stale
                          ? 'text-emerald-300'
                          : 'text-amber-300'
                    }`}
                  >
                    {!available ? 'NOT ATTACHED' : alive ? (stale ? 'STALE' : 'ALIVE') : 'DOWN'}
                  </Badge>
                </div>
                <div className="mt-1 truncate font-mono text-[10px] text-foreground">
                  {row.worker}
                </div>
                <div className="mt-1 flex items-baseline justify-between gap-2">
                  <span className="font-mono text-[10px] tabular-nums text-muted-foreground">
                    PID{' '}
                    <span className="text-foreground">{ring?.pid ?? '—'}</span>
                  </span>
                  <span className="flex items-center gap-1 font-mono text-[10px] tabular-nums text-muted-foreground">
                    <HeartPulse
                      className={`size-3 ${
                        stale ? 'text-amber-300' : alive ? 'text-emerald-300' : 'text-muted-foreground'
                      }`}
                    />
                    {beatAge === null || beatAge === undefined
                      ? '—'
                      : `${beatAge.toFixed(1)} ms`}
                  </span>
                </div>
                <div className="mt-1 truncate text-[9px] text-muted-foreground">
                  {row.role}
                </div>
              </div>
            )
          })}
        </div>

        {!available ? (
          <div className="rounded-md border border-dashed border-border/60 bg-muted/10 px-3 py-2 text-[10px] text-muted-foreground">
            Supervisor not attached: {rings?.reason ?? 'single-process mode'}. Press{' '}
            <span className="font-mono">START 4 RINGS</span> above (or launch with{' '}
            <span className="font-mono">python scripts/run_nios.py --multiprocess</span>)
            to prove four distinct PIDs.
          </div>
        ) : null}

        <div className="grid gap-2 sm:grid-cols-4">
          {Object.entries(siloModes).length === 0 ? (
            <div className="sm:col-span-4 rounded-md border border-border/60 bg-background/40 px-3 py-2 text-[10px] text-muted-foreground">
              No live dual-vendor cycle yet — toggle the feed switch in the top bar.
            </div>
          ) : (
            Object.entries(siloModes).map(([silo, mode]) => (
              <div
                key={silo}
                className="rounded-md border border-border/60 bg-background/40 px-2 py-1.5"
              >
                <div className="truncate text-[9px] uppercase tracking-wider text-muted-foreground">
                  {silo}
                </div>
                <div
                  className={`font-mono text-[10px] ${
                    mode === 'LIVE_PUBLIC_DUAL_VENDOR'
                      ? 'text-emerald-300'
                      : 'text-amber-300'
                  }`}
                >
                  {mode === 'LIVE_PUBLIC_DUAL_VENDOR' ? 'LIVE' : 'SYNTHETIC'}
                </div>
              </div>
            ))
          )}
        </div>

        {lastCycle ? (
          <div className="font-mono text-[10px] text-muted-foreground">
            last cycle · {lastCycle.frames ?? 0} frames ·{' '}
            <span className="text-emerald-300">{lastCycle.accepted ?? 0} accepted</span> ·{' '}
            {lastCycle.rejected ?? 0} rejected · round {lastCycle.round ?? 0}
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}

/* ------------------------------------------------------------------ *
 * Formal verification & VOPR badge
 * ------------------------------------------------------------------ */

function FormalVerificationBanner({
  formal,
  onRefreshed,
}: {
  formal: FormalProofState | null
  onRefreshed: (next: FormalProofState) => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const tla = formal?.tla ?? null
  const vopr = formal?.vopr ?? null
  const states = tla?.total_states ?? 0
  const transitions = tla?.total_transitions ?? 0
  const counterexamples = tla?.total_counterexamples ?? 0
  const voprSteps = vopr?.steps ?? 0
  const overdrafts = vopr?.overdraft_violations ?? 0
  const faults = Object.values(vopr?.fault_counts ?? {}).reduce(
    (sum, value) => sum + value,
    0
  )
  const proven =
    Boolean(tla?.ok) &&
    Boolean(vopr?.ok) &&
    counterexamples === 0 &&
    overdrafts === 0

  const reverify = async () => {
    setBusy(true)
    setError(null)
    try {
      onRefreshed(await fetchFormalProofs(true))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'formal re-verification failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card
      className={`gap-3 py-4 shadow-none ${
        proven
          ? 'border-emerald-500/40 bg-emerald-500/[0.04]'
          : 'border-amber-500/40 bg-amber-500/[0.04]'
      }`}
    >
      <CardContent className="space-y-3 px-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-start gap-3">
            <div
              className={`mt-0.5 rounded-md p-1.5 ${
                proven ? 'bg-emerald-500/15 text-emerald-300' : 'bg-amber-500/15 text-amber-300'
              }`}
            >
              {proven ? (
                <ShieldCheck className="size-4" />
              ) : (
                <ShieldQuestion className="size-4" />
              )}
            </div>
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold">
                  Formal Verification &amp; VOPR
                </span>
                <Badge
                  variant={proven ? 'secondary' : 'destructive'}
                  className="text-[9px] tracking-wider"
                >
                  {proven ? 'PROVEN · 0 COUNTEREXAMPLES' : 'UNPROVEN'}
                </Badge>
                <Badge variant="outline" className="font-mono text-[9px]">
                  exhaustive BFS
                </Badge>
              </div>
              <p className="mt-1 text-[11px] text-muted-foreground">
                Four TLA+ specifications model-checked to a fixed point, then the
                TigerBeetle outbox replayed under deterministic fault injection.
              </p>
            </div>
          </div>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => void reverify()}
            className="gap-1.5"
          >
            <RefreshCw className={`size-3.5 ${busy ? 'animate-spin' : ''}`} />
            {busy ? 'Re-running BFS…' : 'Re-verify'}
          </Button>
        </div>

        <div className="grid gap-2 sm:grid-cols-2">
          <div className="rounded-md border border-border/60 bg-background/40 px-3 py-2">
            <div className="flex items-center gap-1.5 text-[9px] uppercase tracking-wider text-muted-foreground">
              <GitBranch className="size-3" />
              TLA+ state space
            </div>
            <div className="mt-1 flex items-baseline gap-1.5">
              <span className="font-mono text-lg font-semibold tabular-nums text-foreground">
                {states.toLocaleString('en-US')}
              </span>
              <span className="text-[10px] text-muted-foreground">
                states · {transitions.toLocaleString('en-US')} transitions
              </span>
            </div>
            <div className="mt-1 font-mono text-[10px] text-muted-foreground">
              counterexamples{' '}
              <span className={counterexamples === 0 ? 'text-emerald-300' : 'text-red-300'}>
                {counterexamples}
              </span>{' '}
              · {tla?.specs.length ?? 0}/4 specs
            </div>
          </div>

          <div className="rounded-md border border-border/60 bg-background/40 px-3 py-2">
            <div className="flex items-center gap-1.5 text-[9px] uppercase tracking-wider text-muted-foreground">
              <Waves className="size-3" />
              VOPR fault injection
            </div>
            <div className="mt-1 flex items-baseline gap-1.5">
              <span className="font-mono text-lg font-semibold tabular-nums text-foreground">
                {voprSteps.toLocaleString('en-US')}
              </span>
              <span className="text-[10px] text-muted-foreground">
                steps · {faults.toLocaleString('en-US')} faults injected
              </span>
            </div>
            <div className="mt-1 font-mono text-[10px] text-muted-foreground">
              overdrafts{' '}
              <span className={overdrafts === 0 ? 'text-emerald-300' : 'text-red-300'}>
                {overdrafts}
              </span>{' '}
              · at-most-once{' '}
              <span className={vopr?.at_most_once_ok ? 'text-emerald-300' : 'text-red-300'}>
                {vopr?.at_most_once_ok ? 'held' : 'violated'}
              </span>
            </div>
          </div>
        </div>

        <div className="flex flex-wrap gap-1.5">
          {(tla?.specs ?? []).map((spec) => (
            <span
              key={spec.name}
              title={`${spec.invariants.join(', ')} · ${spec.transitions.toLocaleString('en-US')} transitions`}
              className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[9px] ${
                spec.ok
                  ? 'border-emerald-500/40 text-emerald-300'
                  : 'border-red-500/40 text-red-300'
              }`}
            >
              <CheckCircle2 className="size-2.5" />
              {spec.name}
              <span className="text-muted-foreground">
                {spec.states_explored.toLocaleString('en-US')}
              </span>
            </span>
          ))}
          {(formal?.tigerbeetle || formal?.postgres) ? (
            <span className="ml-auto font-mono text-[9px] text-muted-foreground">
              TigerBeetle {formal?.tigerbeetle?.ready ? 'ready' : 'down'} · PostgreSQL 16{' '}
              {formal?.postgres?.ready ? 'ready' : 'down'}
            </span>
          ) : null}
        </div>

        {error ? (
          <div className="flex items-center gap-1.5 rounded-md border border-red-500/30 bg-red-500/10 px-2 py-1 text-[10px] text-red-200">
            <AlertTriangle className="size-3" />
            {error}
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}

/* ------------------------------------------------------------------ *
 * TacticalVault options tail-hedger
 * ------------------------------------------------------------------ */

function TailHedgerCard({ hedger }: { hedger: TailHedgerState | null }) {
  const plan = hedger?.last_plan ?? null
  const status = hedger?.last_status ?? null
  const fragility = status?.cascade_fragility_index ?? 0
  const copula = status?.tail_correlation ?? 0
  const fragilityTrigger =
    status?.thresholds.cascade_fragility_index ??
    hedger?.thresholds.cascade_fragility_index ??
    DEFAULT_FRAGILITY_TRIGGER
  const copulaTrigger =
    status?.thresholds.tail_correlation ??
    hedger?.thresholds.tail_correlation ??
    DEFAULT_COPULA_TRIGGER
  const armed = hedger !== null
  const triggered = Boolean(status?.triggered)
  const hedged = Boolean(plan)
  const groundingOk = Boolean(plan?.grounding_verified)
  const digest = plan?.packet.sha256_digest ?? null

  const state = !armed
    ? { label: 'NOT ATTACHED', tone: 'border-border text-muted-foreground', variant: 'outline' as const }
    : hedged
      ? { label: 'HEDGE ARMED', tone: 'border-emerald-500/40 text-emerald-300', variant: 'secondary' as const }
      : triggered
        ? { label: 'TRIGGERED · NO SIZE', tone: 'border-amber-500/40 text-amber-300', variant: 'outline' as const }
        : { label: 'MONITORING · FLAT', tone: 'border-border text-muted-foreground', variant: 'outline' as const }

  return (
    <Card className="gap-3 border-border/70 bg-card/60 py-4 shadow-none">
      <CardHeader className="px-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            <Waves className="size-4 text-primary" />
            TacticalVault options tail-hedger
            <Badge variant="outline" className="font-mono text-[9px]">
              protective puts
            </Badge>
          </CardTitle>
          <Badge variant={state.variant} className={`text-[9px] tracking-wider ${state.tone}`}>
            {state.label}
          </Badge>
        </div>
        <p className="text-[11px] text-muted-foreground">
          Ring 2 buys out-of-the-money puts inside the TacticalSwing vault when the
          MiroFish cascade and the cross-silo copula both breach. Ring 0 sizes and signs.
        </p>
      </CardHeader>
      <CardContent className="space-y-3 px-4">
        <div className="grid gap-2 sm:grid-cols-2">
          <TriggerGauge
            label="Cascade fragility"
            value={fragility}
            threshold={fragilityTrigger}
            comparator=">="
          />
          <TriggerGauge
            label="Tail copula correlation"
            value={copula}
            threshold={copulaTrigger}
            comparator=">"
          />
        </div>

        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Metric label="Delta" value={plan ? plan.greeks.delta.toFixed(3) : '—'} />
          <Metric label="Gamma" value={plan ? plan.greeks.gamma.toExponential(2) : '—'} />
          <Metric label="Vega" value={plan ? plan.greeks.vega.toFixed(2) : '—'} />
          <Metric
            label="Premium"
            value={plan ? `${plan.premium_bps.toFixed(1)} bps` : '—'}
            danger={
              plan !== null &&
              hedger !== null &&
              plan.premium_bps > hedger.thresholds.max_premium_bps
            }
          />
        </div>

        <div className="grid gap-2 border-t border-border/60 pt-2 sm:grid-cols-3">
          <div className="text-[10px] text-muted-foreground">
            Strike / spot{' '}
            <span className="font-mono text-foreground">
              {plan ? `${plan.strike.toLocaleString('en-US')} / ${plan.spot.toLocaleString('en-US')}` : '—'}
            </span>
          </div>
          <div className="text-[10px] text-muted-foreground">
            Contracts{' '}
            <span className="font-mono text-foreground">
              {plan ? plan.contracts.toLocaleString('en-US') : '—'}
            </span>{' '}
            · hedge ratio{' '}
            <span className="font-mono text-foreground">
              {plan ? formatPct(plan.hedge_ratio) : '—'}
            </span>
          </div>
          <div className="text-[10px] text-muted-foreground">
            Emitted{' '}
            <span className="font-mono text-foreground">{hedger?.hedges_emitted ?? 0}</span>{' '}
            · triggers{' '}
            <span className="font-mono text-foreground">{hedger?.triggers ?? 0}</span>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border/60 bg-muted/20 px-2 py-1.5">
          <span className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
            <Fingerprint className="size-3" />
            Packet SHA-256
          </span>
          <span className="font-mono text-[10px] text-foreground">{truncateDigest(digest)}</span>
          <Badge
            variant="outline"
            className={`text-[9px] ${
              groundingOk
                ? 'border-emerald-400/40 text-emerald-300'
                : 'border-border text-muted-foreground'
            }`}
          >
            <ShieldCheck className="size-3" />
            {groundingOk
              ? `GROUNDED · ${plan?.grounding_claims_checked ?? 0} claims`
              : 'NO OPEN PACKET'}
          </Badge>
        </div>

        {plan?.reasons.length ? (
          <div className="flex flex-wrap gap-1.5">
            {plan.reasons.map((reason) => (
              <span
                key={reason}
                className="rounded border border-amber-500/40 px-1.5 py-0.5 font-mono text-[9px] text-amber-200"
              >
                {reason}
              </span>
            ))}
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}

function TriggerGauge({
  label,
  value,
  threshold,
  comparator,
}: {
  label: string
  value: number
  threshold: number
  comparator: '>=' | '>'
}) {
  const breached = comparator === '>=' ? value >= threshold : value > threshold
  const ratio = threshold > 0 ? value / threshold : 0

  return (
    <div className="rounded-md border border-border/60 bg-muted/20 px-2 py-1.5">
      <div className="flex items-center justify-between text-[9px] uppercase tracking-wider text-muted-foreground">
        <span>{label}</span>
        <span className={breached ? 'font-mono text-amber-300' : 'font-mono'}>
          {comparator} {threshold.toFixed(2)}
        </span>
      </div>
      <div className="mt-1 flex items-center gap-2">
        <Progress
          value={Math.max(0, Math.min(100, ratio * 100))}
          className={`h-1.5 ${breached ? 'text-amber-300' : ''}`}
        />
        <span
          className={`w-12 text-right font-mono text-xs tabular-nums ${
            breached ? 'text-amber-300' : 'text-foreground'
          }`}
        >
          {value.toFixed(2)}
        </span>
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
