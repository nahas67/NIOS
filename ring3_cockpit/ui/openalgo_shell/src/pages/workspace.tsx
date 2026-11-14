import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  ArrowRight,
  Banknote,
  Flame,
  Lock,
  LogOut,
  Play,
  RotateCcw,
  ShieldCheck,
  Square,
  Timer,
  Zap,
} from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ShellChip, ShellRouteStrip, WorkspaceSwitcher } from '@/components/workspace-chrome'
import {
  authApi,
  type AuthTier,
  type LiveWorkspaceState,
  type PaperWorkspaceState,
  type WorkspaceMode,
} from '@/lib/auth'

/** The fifteen pages, workspace-qualified, in sidebar order. */
export const PAPER_ROUTES: { path: string; label: string }[] = [
  { path: '/paper/dashboard', label: 'Dashboard' },
  { path: '/paper/terminal', label: 'Terminal' },
  { path: '/paper/orders', label: 'Orders' },
  { path: '/paper/options', label: 'Options' },
  { path: '/paper/analyzer', label: 'Analyzer' },
  { path: '/paper/quant', label: 'Quant' },
  { path: '/paper/agents', label: 'Agents' },
  { path: '/paper/backtest', label: 'Backtest' },
  { path: '/paper/brain', label: 'Brain' },
  { path: '/paper/forge', label: 'Forge' },
  { path: '/paper/ledger', label: 'Ledger' },
  { path: '/paper/logbook', label: 'Logbook' },
  { path: '/paper/verification', label: 'Verification' },
  { path: '/paper/brokers', label: 'Brokers' },
  { path: '/paper/control', label: 'Control' },
]

export const LIVE_ROUTES: { path: string; label: string }[] = [
  { path: '/live/dashboard', label: 'Dashboard' },
  { path: '/live/terminal', label: 'Terminal' },
  { path: '/live/orders', label: 'Orders' },
  { path: '/live/options', label: 'Options' },
  { path: '/live/analyzer', label: 'Analyzer' },
  { path: '/live/quant', label: 'Quant' },
  { path: '/live/agents', label: 'Agents' },
  { path: '/live/backtest', label: 'Backtest' },
  { path: '/live/brain', label: 'Brain' },
  { path: '/live/forge', label: 'Forge' },
  { path: '/live/ledger', label: 'Ledger' },
  { path: '/live/logbook', label: 'Logbook' },
  { path: '/live/verification', label: 'Verification' },
  { path: '/live/brokers', label: 'Brokers' },
  { path: '/live/control', label: 'Control' },
]

function toUnits(micros: number | undefined): number {
  return (micros ?? 0) / 1_000_000
}

function formatUnits(value: number): string {
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 })
}

/**
 * PAPER SANDBOX shell — deep obsidian with lavender/cyan accents.
 *
 * This is the only shell allowed to render deposit controls, the clean-slate
 * onboarding panel and the play/pause tape button, because every figure it
 * shows is either zero or the operator's own simulated capital.
 */
export function PaperShell({
  path,
  tier,
  onNavigate,
  children,
}: {
  path: string
  tier: AuthTier
  onNavigate: (path: string) => void
  children: React.ReactNode
}) {
  const [state, setState] = useState<PaperWorkspaceState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [amount, setAmount] = useState('10000')
  const [silo, setSilo] = useState('US_IBKR')
  const [busy, setBusy] = useState(false)
  const [confirmReset, setConfirmReset] = useState(false)
  const page = path.split('/').at(-1) ?? 'dashboard'

  const refresh = useCallback(async () => {
    try {
      const next = await authApi.paperState()
      setState(next)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'paper workspace unavailable')
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const navUnits = useMemo(() => {
    if (!state) return 0
    return Object.values(state.silos).reduce(
      (total, entry) => total + toUnits(entry.nav_micros),
      0,
    )
  }, [state])

  const siloIds = useMemo(() => (state ? Object.keys(state.silos).sort() : []), [state])

  async function deposit() {
    setBusy(true)
    setError(null)
    try {
      await authApi.fundPaper({
        silo,
        amount_units: Number(amount),
        vault: 'ALL_CANONICAL',
      })
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'deposit failed')
    } finally {
      setBusy(false)
    }
  }

  async function resetAll() {
    if (!confirmReset) {
      setConfirmReset(true)
      return
    }
    setBusy(true)
    setError(null)
    try {
      await authApi.resetWorkspace()
      setConfirmReset(false)
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'reset failed')
    } finally {
      setBusy(false)
    }
  }

  async function toggleAutopilot() {
    if (!state) return
    setBusy(true)
    try {
      await authApi.setAutopilot(!state.autopilot.enabled)
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'autopilot toggle failed')
    } finally {
      setBusy(false)
    }
  }

  const cleanSlate = state?.clean_slate_active ?? false
  const showOnboarding = cleanSlate && (page === 'dashboard' || page === 'ledger')

  return (
    <div className="nios-shell-paper flex min-h-0 flex-1 flex-col">
      {/* ---- top ribbon: sandbox identity, simulated NAV, tape + capital ---- */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-cyan-500/30 bg-gradient-to-r from-cyan-950/50 via-violet-950/40 to-transparent px-4 py-2 lg:px-6">
        <div className="flex flex-wrap items-center gap-2">
          <ShellChip tone="paper">
            <ShieldCheck className="size-3" />
            PAPER SANDBOX
          </ShellChip>
          <ShellChip tone="neutral">
            SIMULATED NAV <span className="nios-tabular ml-1">{formatUnits(navUnits)}</span>
          </ShellChip>
          <ShellChip tone="neutral">
            SHADOW {state?.shadow.shadow_fills ?? 0}/{state?.shadow.required_fills ?? 30}
          </ShellChip>
          <ShellChip tone={cleanSlate ? 'paper' : 'neutral'}>
            {cleanSlate ? 'CLEAN SLATE · ALL VALUES 0' : 'FUNDED'}
          </ShellChip>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant={state?.autopilot.enabled ? 'secondary' : 'default'}
            onClick={toggleAutopilot}
            disabled={busy}
          >
            {state?.autopilot.enabled ? (
              <>
                <Square className="mr-1 size-3" /> Autopilot: pause
              </>
            ) : (
              <>
                <Play className="mr-1 size-3" /> Autopilot: play
              </>
            )}
          </Button>
          <Button
            size="sm"
            variant={confirmReset ? 'destructive' : 'outline'}
            onClick={resetAll}
            disabled={busy}
          >
            <RotateCcw className="mr-1 size-3" />
            {confirmReset ? 'Confirm reset to zero' : 'Reset to zero'}
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="border-rose-500/50 text-rose-200 hover:bg-rose-500/10"
            onClick={() => onNavigate('/live/login')}
          >
            <Lock className="mr-1 size-3" /> Switch to live vault
          </Button>
        </div>
      </div>

      {/* ---- second row: workspace switcher + the simulated-capital deposit ---- */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/40 px-4 py-2 lg:px-6">
        <div className="flex flex-wrap items-center gap-3">
          <WorkspaceSwitcher mode="paper" tier={tier} onNavigate={onNavigate} />
          <ShellRouteStrip routes={PAPER_ROUTES} path={path} mode="paper" onNavigate={onNavigate} />
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <div className="space-y-1">
            <Label htmlFor="paper-fund-silo" className="text-[9px] uppercase tracking-wide">
              Silo
            </Label>
            <select
              id="paper-fund-silo"
              value={silo}
              onChange={(event) => setSilo(event.target.value)}
              className="h-8 rounded-md border border-input bg-transparent px-2 text-[11px]"
            >
              {(siloIds.length ? siloIds : ['US_IBKR']).map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="paper-fund-amount" className="text-[9px] uppercase tracking-wide">
              Amount
            </Label>
            <Input
              id="paper-fund-amount"
              className="h-8 w-28"
              inputMode="decimal"
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
            />
          </div>
          <Button size="sm" onClick={deposit} disabled={busy}>
            <Banknote className="mr-1 size-3" /> Deposit paper capital
          </Button>
        </div>
      </div>

      {error ? (
        <div className="mx-4 mt-3 flex items-center gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200 lg:mx-6">
          <AlertTriangle className="size-3.5" />
          {error}
        </div>
      ) : null}

      {showOnboarding ? (
        <section className="nios-glass mx-4 mt-3 px-4 py-3 lg:mx-6">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="flex items-center gap-2 text-sm font-semibold text-cyan-100">
                <Banknote className="size-4" />
                PAPER SANDBOX — simulated execution only (all values start at 0)
              </h2>
              <p className="mt-1 max-w-xl text-[11px] leading-relaxed text-muted-foreground">
                Every silo and vault reads <span className="text-cyan-200">0</span>. Deposit
                your own Paper capital to open a balanced double-entry
                <span className="text-cyan-200"> EQUITY_CAPITAL → VAULTS </span>
                genesis entry. The autopilot stays OFF until you press play.
              </p>
            </div>
            <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {Object.entries(state?.silos ?? {}).map(([id, entry]) => (
                <div key={id} className="rounded-lg border border-border/50 bg-card/40 px-2.5 py-1.5">
                  <dt className="text-[9px] uppercase tracking-wide text-muted-foreground">
                    {id}
                  </dt>
                  <dd className="nios-tabular font-mono text-xs text-foreground">
                    {formatUnits(toUnits(entry.nav_micros))}
                  </dd>
                </div>
              ))}
            </dl>
          </div>
        </section>
      ) : null}

      <div className="px-4 pt-4 lg:px-6">{children}</div>
    </div>
  )
}

/**
 * LIVE PRODUCTION shell — matte carbon with crimson/amber hazard accents.
 *
 * A LIVE session is required to render anything past the sealed door. Inside,
 * this shell deliberately has **no** deposit button, **no** shadow-burst button
 * and **no** paper balance: those are Paper-only affordances and their absence
 * is part of the safety contract, not an oversight.
 */
export function LiveShell({
  path,
  tier,
  expiresNs,
  onNavigate,
  children,
}: {
  path: string
  tier: AuthTier
  expiresNs?: number | null
  onNavigate: (path: string) => void
  children: React.ReactNode
}) {
  const [state, setState] = useState<LiveWorkspaceState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [nowNs, setNowNs] = useState(() => Date.now() * 1_000_000)

  const unlocked = tier === 'LIVE'

  const refresh = useCallback(async () => {
    if (!unlocked) return
    try {
      const next = await authApi.liveState()
      setState(next)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'live workspace locked')
    }
  }, [unlocked])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // A live session is 30 minutes. The countdown is the operator's only clock.
  useEffect(() => {
    if (!unlocked) return
    const timer = window.setInterval(() => setNowNs(Date.now() * 1_000_000), 1_000)
    return () => window.clearInterval(timer)
  }, [unlocked])

  if (!unlocked) {
    return (
      <div className="nios-shell-live flex min-h-0 flex-1 flex-col">
        <div className="nios-hazard flex flex-wrap items-center gap-2 border-b border-rose-500/50 px-4 py-2 text-[11px] text-rose-100 lg:px-6">
          <Lock className="size-3.5" />
          <span className="font-semibold uppercase tracking-[0.18em]">
            LIVE PRODUCTION — VAULT SEALED
          </span>
        </div>
        <div className="mx-auto mt-16 max-w-lg rounded-2xl border border-rose-500/40 bg-rose-500/5 p-6 text-center">
          <Lock className="mx-auto size-6 text-rose-300" />
          <h2 className="mt-3 text-sm font-semibold text-rose-100">
            Live Production is sealed
          </h2>
          <p className="mt-1 text-[11px] text-muted-foreground">
            A LIVE-tier session is required. Paper sessions never see live balances, broker
            credentials or the go-live certificate.
          </p>
          <Button className="mt-4" onClick={() => onNavigate('/live/login')}>
            Enter the live vault
          </Button>
        </div>
      </div>
    )
  }

  const preflight = (state?.preflight ?? {}) as Record<string, unknown>
  const gatesPassed = Number(preflight.gates_passed ?? preflight.passed ?? 0)
  const gatesTotal = Number(preflight.gates_total ?? 22)
  const certificateReady = Boolean(preflight.ready_for_live_cutover ?? false)
  const remainingSeconds =
    typeof expiresNs === 'number' && expiresNs > 0
      ? Math.max(0, Math.floor((expiresNs - nowNs) / 1_000_000_000))
      : null
  const ttlLabel =
    remainingSeconds === null
      ? '30:00'
      : `${String(Math.floor(remainingSeconds / 60)).padStart(2, '0')}:${String(
          remainingSeconds % 60,
        ).padStart(2, '0')}`

  async function flatten() {
    setBusy(true)
    try {
      await authApi.flatten()
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'flatten failed')
    } finally {
      setBusy(false)
    }
  }

  async function lockVault() {
    setBusy(true)
    try {
      await authApi.logout()
    } catch {
      // The cookie is dropped locally either way; the operator lands on paper.
    } finally {
      setBusy(false)
      onNavigate('/paper/login')
    }
  }

  return (
    <div className="nios-shell-live flex min-h-0 flex-1 flex-col">
      {/* ---- hazard ribbon: real-capital identity, certificate, TTL, kill ---- */}
      <div className="nios-hazard flex flex-wrap items-center justify-between gap-2 border-b border-rose-500/50 px-4 py-2 text-[11px] text-rose-50 lg:px-6">
        <div className="flex flex-wrap items-center gap-2">
          <ShellChip tone="live">
            <Lock className="size-3" />
            🔒 LIVE PRODUCTION — REAL CAPITAL
          </ShellChip>
          <ShellChip tone="neutral" className="border-amber-500/50 text-amber-100">
            <ShieldCheck className="mr-1 size-3" />
            22-GATE {certificateReady ? 'CERTIFIED' : 'PENDING'} · {gatesPassed}/{gatesTotal}
          </ShellChip>
          <ShellChip tone="neutral" className="border-rose-500/50 text-rose-100">
            <Timer className="mr-1 size-3" />
            LIVE SESSION TTL {ttlLabel}
          </ShellChip>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="destructive" onClick={flatten} disabled={busy}>
            <Flame className="mr-1 size-3" /> &lt;1ms emergency kill switch
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="border-amber-500/50 text-amber-100 hover:bg-amber-500/10"
            onClick={lockVault}
            disabled={busy}
          >
            <LogOut className="mr-1 size-3" /> Lock live vault (drop to paper)
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/40 px-4 py-2 lg:px-6">
        <div className="flex flex-wrap items-center gap-3">
          <WorkspaceSwitcher mode="live" tier={tier} onNavigate={onNavigate} />
          <ShellRouteStrip routes={LIVE_ROUTES} path={path} mode="live" onNavigate={onNavigate} />
        </div>
        <div className="flex items-center gap-2">
          {state?.silos
            ? Object.entries(state.silos).map(([id, entry]) => (
                <ShellChip key={id} tone="neutral">
                  {id} {String(entry.mode ?? 'PAPER')}
                </ShellChip>
              ))
            : null}
        </div>
      </div>

      {error ? (
        <div className="mx-4 mt-3 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200 lg:mx-6">
          {error}
        </div>
      ) : null}

      <section className="mx-4 mt-3 grid gap-3 lg:mx-6 lg:grid-cols-3">
        <div className="nios-glass border-rose-500/30 p-3">
          <h2 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-rose-100">
            <ShieldCheck className="size-3.5" /> 22-Gate Go-Live Certificate
          </h2>
          <p className="nios-tabular mt-2 font-mono text-[11px] text-muted-foreground">
            status: {String(preflight.status ?? (certificateReady ? 'READY' : 'PENDING'))}
          </p>
          <p className="nios-tabular font-mono text-[11px] text-muted-foreground">
            gates: {gatesPassed} / {gatesTotal}
          </p>
          <p className="nios-tabular font-mono text-[11px] text-muted-foreground">
            failed: {String(preflight.failed_gates ?? 0)}
          </p>
        </div>

        <div className="nios-glass border-amber-500/30 p-3">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-amber-100">
            Write-Only Venue Credentials
          </h2>
          <ul className="mt-2 space-y-1 text-[11px] text-muted-foreground">
            {Object.entries(
              ((state?.broker_credentials as { silos?: Record<string, { configured?: number }> })
                ?.silos ?? {}) as Record<string, { configured?: number }>,
            )
              .slice(0, 12)
              .map(([id, entry]) => (
                <li key={id} className="flex items-center justify-between gap-2">
                  <span className="font-mono">{id}</span>
                  <span
                    className={entry.configured ? 'text-emerald-300' : 'text-muted-foreground'}
                  >
                    {entry.configured ? 'configured · ********' : 'unset'}
                  </span>
                </li>
              ))}
            {Object.keys(
              ((state?.broker_credentials as { silos?: Record<string, unknown> })?.silos ?? {}) as
                Record<string, unknown>,
            ).length === 0 ? (
              <li>no credential handshake recorded</li>
            ) : null}
          </ul>
        </div>

        <div className="nios-glass p-3">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-foreground">
            Dual-Vendor Feed &amp; Arming
          </h2>
          <p className="nios-tabular mt-2 font-mono text-[11px] text-muted-foreground">
            feed mode: {String((state?.feed as { feed_mode?: string })?.feed_mode ?? 'UNKNOWN')}
          </p>
          <p className="nios-tabular font-mono text-[11px] text-muted-foreground">
            live fills: {state?.live_fills ?? 0}
          </p>
          <p className="mt-2 flex items-center gap-1 text-[10px] text-rose-200/80">
            <Zap className="size-3" /> Every entry still passes the 98% conformal gate.
          </p>
          <Button
            size="sm"
            variant="outline"
            className="mt-2 border-rose-500/40 text-rose-100"
            onClick={() => onNavigate('/live/brokers')}
          >
            <ArrowRight className="mr-1 size-3" /> Open the broker hub
          </Button>
        </div>
      </section>

      <div className="px-4 pt-4 lg:px-6">{children}</div>
    </div>
  )
}

/** Backwards-compatible aliases used by the App router. */
export const PaperWorkspacePage = PaperShell
export const LiveWorkspacePage = LiveShell

export type { WorkspaceMode }
