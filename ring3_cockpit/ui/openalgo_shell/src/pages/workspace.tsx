import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  ArrowRight,
  Banknote,
  Flame,
  Lock,
  Play,
  RotateCcw,
  ShieldCheck,
  Square,
} from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { WorkspaceBanner, WorkspaceSwitcher } from '@/components/workspace-chrome'
import {
  authApi,
  type AuthTier,
  type LiveWorkspaceState,
  type PaperWorkspaceState,
  type WorkspaceMode,
} from '@/lib/auth'
import { cn } from '@/lib/utils'

export const PAPER_ROUTES: { path: string; label: string }[] = [
  { path: '/paper/dashboard', label: 'Dashboard' },
  { path: '/paper/orders', label: 'Orders' },
  { path: '/paper/analyzer', label: 'Analyzer' },
  { path: '/paper/quant', label: 'Quant' },
  { path: '/paper/brain', label: 'Brain' },
  { path: '/paper/forge', label: 'Forge' },
  { path: '/paper/ledger', label: 'Ledger' },
  { path: '/paper/verification', label: 'Verification' },
  { path: '/paper/control', label: 'Control' },
  { path: '/paper/agents', label: 'Agents' },
  { path: '/paper/backtest', label: 'Backtest' },
]

export const LIVE_ROUTES: { path: string; label: string }[] = [
  { path: '/live/dashboard', label: 'Dashboard' },
  { path: '/live/orders', label: 'Orders' },
  { path: '/live/positions', label: 'Positions' },
  { path: '/live/quant', label: 'Quant' },
  { path: '/live/brain', label: 'Brain' },
  { path: '/live/ledger', label: 'Ledger' },
  { path: '/live/control', label: 'Control' },
  { path: '/live/agents', label: 'Agents' },
  { path: '/live/backtest', label: 'Backtest' },
]

function WorkspaceNav({
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

function toUnits(micros: number | undefined): number {
  return (micros ?? 0) / 1_000_000
}

export function PaperWorkspacePage({
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

  const siloIds = useMemo(
    () => (state ? Object.keys(state.silos).sort() : []),
    [state],
  )

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
  const showOnboarding =
    cleanSlate && (page === 'dashboard' || page === 'ledger')

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <WorkspaceBanner
        mode="paper"
        cleanSlate={cleanSlate}
        navUnits={navUnits}
        shadowFills={state?.shadow.shadow_fills ?? 0}
        autopilot={state?.autopilot.enabled ?? false}
        tier={tier}
      />
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/40 px-4 py-2 lg:px-6">
        <div className="flex items-center gap-3">
          <WorkspaceSwitcher mode="paper" tier={tier} onNavigate={onNavigate} />
          <WorkspaceNav
            routes={PAPER_ROUTES}
            path={path}
            mode="paper"
            onNavigate={onNavigate}
          />
        </div>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant={state?.autopilot.enabled ? 'secondary' : 'default'}
            onClick={toggleAutopilot}
            disabled={busy}
          >
            {state?.autopilot.enabled ? (
              <>
                <Square className="mr-1 size-3" /> Pause autopilot
              </>
            ) : (
              <>
                <Play className="mr-1 size-3" /> Start paper autopilot
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
            {confirmReset ? 'Confirm reset to zero' : 'Reset all values to zero'}
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
        <section className="mx-4 mt-3 rounded-xl border border-cyan-500/30 bg-cyan-500/5 px-4 py-3 lg:mx-6">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="flex items-center gap-2 text-sm font-semibold text-cyan-100">
                <Banknote className="size-4" />
                Clean slate — nothing pre-funded
              </h2>
              <p className="mt-1 max-w-xl text-[11px] leading-relaxed text-muted-foreground">
                Every silo and vault reads <span className="text-cyan-200">0</span>. Deposit
                your own Paper capital to open a balanced double-entry
                <span className="text-cyan-200"> EQUITY_CAPITAL → VAULTS </span>
                genesis entry. The autopilot stays OFF until you press start.
              </p>
            </div>
            <div className="flex flex-wrap items-end gap-2">
              <div className="space-y-1">
                <Label htmlFor="fund-silo" className="text-[10px] uppercase tracking-wide">
                  Silo
                </Label>
                <select
                  id="fund-silo"
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
                <Label htmlFor="fund-amount" className="text-[10px] uppercase tracking-wide">
                  Amount
                </Label>
                <Input
                  id="fund-amount"
                  className="h-8 w-32"
                  inputMode="decimal"
                  value={amount}
                  onChange={(event) => setAmount(event.target.value)}
                />
              </div>
              <Button size="sm" onClick={deposit} disabled={busy}>
                <ArrowRight className="mr-1 size-3" /> Deposit starting capital
              </Button>
            </div>
          </div>
          <dl className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
            {Object.entries(state?.silos ?? {}).map(([id, entry]) => (
              <div
                key={id}
                className="rounded-lg border border-border/50 bg-card/40 px-2.5 py-1.5"
              >
                <dt className="text-[9px] uppercase tracking-wide text-muted-foreground">
                  {id}
                </dt>
                <dd className="font-mono text-xs text-foreground">
                  {toUnits(entry.nav_micros).toLocaleString(undefined, {
                    maximumFractionDigits: 2,
                  })}
                </dd>
              </div>
            ))}
          </dl>
        </section>
      ) : null}

      <div className="px-4 pt-4 lg:px-6">{children}</div>
    </div>
  )
}

export function LiveWorkspacePage({
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
  const [state, setState] = useState<LiveWorkspaceState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

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

  if (!unlocked) {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <WorkspaceBanner mode="live" tier={tier} />
        <div className="mx-auto mt-16 max-w-lg rounded-2xl border border-rose-500/40 bg-rose-500/5 p-6 text-center">
          <Lock className="mx-auto size-6 text-rose-300" />
          <h2 className="mt-3 text-sm font-semibold text-rose-100">
            Live Production is sealed
          </h2>
          <p className="mt-1 text-[11px] text-muted-foreground">
            A LIVE-tier session is required. Paper sessions never see live balances,
            broker credentials or the go-live certificate.
          </p>
          <Button className="mt-4" onClick={() => onNavigate('/live-unlock')}>
            Enter live PIN
          </Button>
        </div>
      </div>
    )
  }

  const preflight = (state?.preflight ?? {}) as Record<string, unknown>
  const credentialSilos = ((state?.broker_credentials as { silos?: Record<string, { configured?: number }> })
    ?.silos ?? {}) as Record<string, { configured?: number }>
  const feed = (state?.feed ?? {}) as Record<string, unknown>
  const feedRows = (feed.rows ?? feed.vendors ?? []) as unknown[]

  async function arm(mode: 'LIVE' | 'PAPER') {
    setBusy(true)
    try {
      await authApi.armSilo({ silo: 'US_IBKR', mode, confirm_live: true })
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'mode change failed')
    } finally {
      setBusy(false)
    }
  }

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

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <WorkspaceBanner mode="live" tier={tier} />
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/40 px-4 py-2 lg:px-6">
        <div className="flex items-center gap-3">
          <WorkspaceSwitcher mode="live" tier={tier} onNavigate={onNavigate} />
          <WorkspaceNav
            routes={LIVE_ROUTES}
            path={path}
            mode="live"
            onNavigate={onNavigate}
          />
        </div>
        <Button size="sm" variant="destructive" onClick={flatten} disabled={busy}>
          <Flame className="mr-1 size-3" /> Emergency flatten (&lt;1 ms)
        </Button>
      </div>

      {error ? (
        <div className="mx-4 mt-3 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200 lg:mx-6">
          {error}
        </div>
      ) : null}

      <section className="mx-4 mt-3 grid gap-3 lg:mx-6 lg:grid-cols-3">
        <div className="rounded-xl border border-rose-500/30 bg-card/40 p-3">
          <h2 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-rose-100">
            <ShieldCheck className="size-3.5" /> 22-Gate Go-Live Certificate
          </h2>
          <p className="mt-2 font-mono text-[11px] text-muted-foreground">
            status: {String(preflight.status ?? preflight.ready ?? 'UNKNOWN')}
          </p>
          <p className="font-mono text-[11px] text-muted-foreground">
            gates: {String(preflight.gates_passed ?? preflight.passed ?? '—')} /{' '}
            {String(preflight.gates_total ?? 22)}
          </p>
        </div>

        <div className="rounded-xl border border-amber-500/30 bg-card/40 p-3">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-amber-100">
            12-Venue Write-Only Credential Vault
          </h2>
          <ul className="mt-2 space-y-1 text-[11px] text-muted-foreground">
            {Object.entries(credentialSilos).length ? (
              Object.entries(credentialSilos)
                .slice(0, 12)
                .map(([id, entry]) => (
                  <li key={id} className="flex items-center justify-between gap-2">
                    <span className="font-mono">{id}</span>
                    <span className={entry.configured ? 'text-emerald-300' : 'text-muted-foreground'}>
                      {entry.configured ? 'configured · ********' : 'unset'}
                    </span>
                  </li>
                ))
            ) : (
              <li>no credential handshake recorded</li>
            )}
          </ul>
        </div>

        <div className="rounded-xl border border-border/40 bg-card/40 p-3">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-foreground">
            Dual-Vendor Feed
          </h2>
          <p className="mt-2 font-mono text-[11px] text-muted-foreground">
            mode: {String(feed.feed_mode ?? 'UNKNOWN')}
          </p>
          <p className="font-mono text-[11px] text-muted-foreground">
            vendors tracked: {feedRows.length}
          </p>
          <div className="mt-2 flex gap-2">
            <Button size="sm" variant="destructive" onClick={() => arm('LIVE')} disabled={busy}>
              Arm LIVE
            </Button>
            <Button size="sm" variant="outline" onClick={() => arm('PAPER')} disabled={busy}>
              Disarm to PAPER
            </Button>
          </div>
        </div>
      </section>

      <div className="px-4 pt-4 lg:px-6">{children}</div>
    </div>
  )
}
