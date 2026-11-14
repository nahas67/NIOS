import { type FormEvent, useEffect, useState } from 'react'
import {
  ArrowRight,
  Check,
  KeyRound,
  Layers,
  Lock,
  ShieldCheck,
  ShieldHalf,
} from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { authApi, type AuthSession, type AuthStatus } from '@/lib/auth'
import { cn } from '@/lib/utils'

interface PortalProps {
  status: AuthStatus | null
  onAuthenticated: (session: AuthSession) => Promise<void>
  onDone: (path: string) => void
}

/** Shared obsidian backdrop with a per-portal accent wash. */
function PortalBackdrop({
  tone,
  children,
}: {
  tone: 'gateway' | 'paper' | 'live'
  children: React.ReactNode
}) {
  const wash =
    tone === 'live'
      ? 'radial-gradient(60rem 30rem at 16% -5%, rgba(244,63,94,0.26), transparent 66%), radial-gradient(50rem 26rem at 88% 8%, rgba(245,158,11,0.20), transparent 66%)'
      : tone === 'paper'
        ? 'radial-gradient(60rem 30rem at 14% -5%, rgba(167,139,250,0.24), transparent 66%), radial-gradient(50rem 26rem at 86% 6%, rgba(34,211,238,0.20), transparent 66%)'
        : 'radial-gradient(60rem 30rem at 12% 0%, rgba(56,189,248,0.16), transparent 66%), radial-gradient(50rem 26rem at 88% 10%, rgba(168,85,247,0.14), transparent 66%)'
  return (
    <div className="dark">
      <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-[#06060a] px-4 py-12 text-foreground">
        <div aria-hidden className="pointer-events-none absolute inset-0" style={{ background: wash }} />
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 opacity-40"
          style={{
            backgroundImage:
              'linear-gradient(rgba(148,163,184,0.06) 1px, transparent 1px), linear-gradient(90deg, rgba(148,163,184,0.06) 1px, transparent 1px)',
            backgroundSize: '48px 48px',
          }}
        />
        <div className="relative z-10 w-full">{children}</div>
      </div>
    </div>
  )
}

/**
 * `/login` — the dual-portal gateway selector.
 *
 * Two cards, never one form.  The operator must choose which *system* they are
 * entering before they type a secret, because the two portals mint different
 * tiers with different lifetimes and reach different state.
 */
export function LoginGatewayPage({ status, onDone }: PortalProps) {
  const initialized = Boolean(status?.initialized)
  return (
    <PortalBackdrop tone="gateway">
      <div className="mx-auto w-full max-w-5xl">
        <header className="mb-8 text-center">
          <div className="mx-auto mb-3 flex size-11 items-center justify-center rounded-2xl border border-border/60 bg-card/70 backdrop-blur">
            <Layers className="size-5 text-sky-300" />
          </div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.28em] text-muted-foreground">
            NIOS · Ring 3 · sovereign gateway
          </p>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight">
            Choose your execution environment
          </h1>
          <p className="mx-auto mt-2 max-w-2xl text-[11px] leading-relaxed text-muted-foreground">
            The two portals are physically separate login flows. Paper issues a 12-hour
            sandbox session that cannot reach live state. Live issues a 30-minute session in
            one three-factor request and is the only way to reach real capital.
          </p>
        </header>

        <div className="nios-portal-grid">
          {/* ---- PAPER SANDBOX portal ---- */}
          <section className="nios-glass nios-glass-strong border-cyan-500/30 p-6">
            <div className="flex items-center gap-3">
              <span className="flex size-10 items-center justify-center rounded-xl border border-cyan-400/40 bg-cyan-500/10 text-cyan-200">
                <ShieldCheck className="size-5" />
              </span>
              <div>
                <p className="text-[9px] font-semibold uppercase tracking-[0.22em] text-cyan-300/80">
                  Simulated execution only
                </p>
                <h2 className="text-lg font-semibold tracking-tight">PAPER SANDBOX</h2>
              </div>
            </div>
            <p className="mt-3 text-[11px] leading-relaxed text-muted-foreground">
              Research, backtests, the shadow nursery and the full 15-page workstation against
              your own simulated capital. Every value starts at zero.
            </p>
            <ul className="mt-3 space-y-1 text-[11px] text-muted-foreground">
              <li className="flex items-center gap-1.5">
                <Check className="size-3 text-cyan-300" /> 12-hour PAPER-tier session
              </li>
              <li className="flex items-center gap-1.5">
                <Check className="size-3 text-cyan-300" /> Cannot reach live balances or keys
              </li>
              <li className="flex items-center gap-1.5">
                <Check className="size-3 text-cyan-300" /> Deposit / reset simulated capital freely
              </li>
            </ul>
            <Button
              className="mt-5 w-full bg-cyan-500/90 text-slate-950 hover:bg-cyan-400"
              onClick={() => onDone('/paper/login')}
            >
              Enter paper sandbox <ArrowRight className="ml-1 size-4" />
            </Button>
          </section>

          {/* ---- LIVE SOVEREIGN VAULT portal ---- */}
          <section className="nios-glass nios-glass-strong border-rose-500/40 p-6">
            <div className="flex items-center gap-3">
              <span className="flex size-10 items-center justify-center rounded-xl border border-rose-400/40 bg-rose-500/10 text-rose-200">
                <Lock className="size-5" />
              </span>
              <div>
                <p className="text-[9px] font-semibold uppercase tracking-[0.22em] text-rose-300/80">
                  Real capital · armoured
                </p>
                <h2 className="text-lg font-semibold tracking-tight">LIVE SOVEREIGN VAULT</h2>
              </div>
            </div>
            <p className="mt-3 text-[11px] leading-relaxed text-muted-foreground">
              Production silos, venue credentials, the 22-gate certificate and the kill switch.
              Requires the password <em>and</em> the six-digit live elevation PIN in one request.
            </p>
            <ul className="mt-3 space-y-1 text-[11px] text-muted-foreground">
              <li className="flex items-center gap-1.5">
                <ShieldHalf className="size-3 text-rose-300" /> 30-minute LIVE-tier session
              </li>
              <li className="flex items-center gap-1.5">
                <ShieldHalf className="size-3 text-rose-300" /> Three factors: handle + password + PIN
              </li>
              <li className="flex items-center gap-1.5">
                <ShieldHalf className="size-3 text-rose-300" /> Real-capital risk acknowledgement
              </li>
            </ul>
            <Button
              className="mt-5 w-full bg-rose-600 text-rose-50 hover:bg-rose-500"
              onClick={() => onDone('/live/login')}
            >
              Enter live sovereign vault <ArrowRight className="ml-1 size-4" />
            </Button>
          </section>
        </div>

        <div className="mt-6 flex flex-wrap items-center justify-between gap-2 text-[10px] text-muted-foreground">
          <span>
            {initialized
              ? 'Operator identity found in the Ring 0 vault.'
              : 'No operator yet — create the sovereign identity once, then use either portal.'}
          </span>
          <span className="flex items-center gap-3">
            <button type="button" className="underline" onClick={() => onDone('/setup')}>
              {initialized ? 'Re-run setup' : 'Create operator identity'}
            </button>
            <button type="button" className="underline" onClick={() => onDone('/live-unlock')}>
              Legacy two-step live unlock
            </button>
          </span>
        </div>
      </div>
    </PortalBackdrop>
  )
}

/**
 * `/paper/login` — the cyan/lavender research portal.
 *
 * Username and password only.  The token it mints is PAPER-tier, so the live
 * workspace answers `403 LIVE_ELEVATION_REQUIRED` by design.
 */
export function PaperLoginPage({ status, onAuthenticated, onDone }: PortalProps) {
  const [username, setUsername] = useState(status?.username ?? '')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const session = await authApi.loginPaper({ username, password })
      await onAuthenticated(session)
      onDone('/paper/dashboard')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'paper login failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <PortalBackdrop tone="paper">
      <div className="mx-auto w-full max-w-md">
        <div className="nios-glass nios-glass-strong border-cyan-500/30 p-7">
          <div className="mb-5 flex items-center gap-3">
            <span className="flex size-10 items-center justify-center rounded-xl border border-cyan-400/40 bg-cyan-500/10 text-cyan-200">
              <ShieldCheck className="size-5" />
            </span>
            <div>
              <p className="text-[10px] font-semibold uppercase tracking-[0.2em] text-cyan-300/80">
                Paper research portal
              </p>
              <h1 className="text-lg font-semibold tracking-tight">Enter the sandbox</h1>
            </div>
          </div>

          <div className="mb-5 rounded-lg border border-cyan-400/40 bg-cyan-500/10 px-3 py-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-cyan-100">
            PAPER SANDBOX — SIMULATED EXECUTION ONLY (ALL VALUES START AT 0)
          </div>

          <form className="space-y-4" onSubmit={submit}>
            <div className="space-y-1.5">
              <Label htmlFor="paper-login-username">Operator handle</Label>
              <Input
                id="paper-login-username"
                autoComplete="username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                required
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="paper-login-password">Password</Label>
              <Input
                id="paper-login-password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                required
              />
            </div>
            {error ? (
              <p className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-[11px] text-destructive">
                {error}
              </p>
            ) : null}
            <Button
              type="submit"
              className="w-full bg-cyan-500/90 text-slate-950 hover:bg-cyan-400"
              disabled={busy}
            >
              {busy ? 'Opening sandbox…' : 'Enter Paper Sandbox'}
            </Button>
          </form>

          <div className="mt-5 flex items-center justify-between text-[11px] text-muted-foreground">
            <button type="button" className="underline" onClick={() => onDone('/login')}>
              Back to gateway
            </button>
            <button
              type="button"
              className="underline text-rose-300"
              onClick={() => onDone('/live/login')}
            >
              Live vault →
            </button>
          </div>
          <p className="mt-5 text-[10px] leading-relaxed text-muted-foreground">
            Ring 0 identity · salted scrypt password digest · HMAC-SHA256 session token · a
            PAPER token can never read live state.
          </p>
        </div>
      </div>
    </PortalBackdrop>
  )
}

/**
 * `/live/login` — the crimson/amber armoured vault door.
 *
 * Three factors in one request plus an explicit real-capital acknowledgement.
 * The 22-gate pre-flight verdict is rendered on the door itself, so an operator
 * can see why LIVE is blocked before typing a PIN.
 */
export function LiveLoginPage({ status, onAuthenticated, onDone }: PortalProps) {
  const [username, setUsername] = useState(status?.username ?? '')
  const [password, setPassword] = useState('')
  const [pin, setPin] = useState('')
  const [acknowledged, setAcknowledged] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [preflight, setPreflight] = useState<Record<string, unknown> | null>(null)

  useEffect(() => {
    let cancelled = false
    authApi
      .preflight()
      .then((report) => {
        if (!cancelled) setPreflight(report)
      })
      .catch(() => {
        if (!cancelled) setPreflight(null)
      })
    return () => {
      cancelled = true
    }
  }, [])

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!acknowledged) {
      setError('Acknowledge the real-capital risk before opening the vault.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const session = await authApi.loginLive({
        username,
        password,
        live_pin: pin,
      })
      await onAuthenticated(session)
      onDone('/live/dashboard')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'live login failed')
    } finally {
      setBusy(false)
    }
  }

  const gatesPassed = Number(preflight?.gates_passed ?? preflight?.passed ?? 0)
  const gatesTotal = Number(preflight?.gates_total ?? 22)
  const ready = Boolean(preflight?.ready_for_live_cutover ?? false)

  return (
    <PortalBackdrop tone="live">
      <div className="mx-auto w-full max-w-md">
        <div className="nios-glass nios-glass-strong border-rose-500/40">
          <div className="nios-hazard rounded-t-[0.9rem] border-b border-rose-500/40 px-5 py-2">
            <p className="text-[10px] font-semibold uppercase tracking-[0.22em] text-rose-50">
              🔒 LIVE PRODUCTION — REAL CAPITAL
            </p>
          </div>

          <div className="p-6">
            <div className="mb-4 flex items-center gap-3">
              <span className="flex size-10 items-center justify-center rounded-xl border border-rose-400/40 bg-rose-500/10 text-rose-200">
                <Lock className="size-5" />
              </span>
              <div>
                <p className="text-[10px] font-semibold uppercase tracking-[0.2em] text-rose-300/80">
                  Sovereign vault door
                </p>
                <h1 className="text-lg font-semibold tracking-tight">Live three-factor entry</h1>
              </div>
            </div>

            <div className="mb-4 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2">
              <p className="flex items-center justify-between text-[10px] uppercase tracking-[0.14em] text-rose-100">
                <span className="flex items-center gap-1.5">
                  <ShieldHalf className="size-3" /> 22-gate pre-flight
                </span>
                <span className={cn('font-mono', ready ? 'text-emerald-300' : 'text-amber-300')}>
                  {gatesPassed}/{gatesTotal} · {ready ? 'CERTIFIED' : 'PENDING'}
                </span>
              </p>
              <p className="mt-1 text-[10px] text-rose-200/70">
                A non-green certificate is surfaced, never hidden. The session still mints at
                30 minutes; every order path re-checks the gates.
              </p>
            </div>

            <form className="space-y-3.5" onSubmit={submit}>
              <div className="space-y-1.5">
                <Label htmlFor="live-login-username">Operator handle</Label>
                <Input
                  id="live-login-username"
                  autoComplete="username"
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  required
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="live-login-password">Password</Label>
                <Input
                  id="live-login-password"
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  required
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="live-login-pin">6-digit live elevation PIN</Label>
                <Input
                  id="live-login-pin"
                  type="password"
                  inputMode="numeric"
                  autoComplete="off"
                  maxLength={12}
                  placeholder="••••••"
                  className="border-rose-500/40 tracking-[0.4em]"
                  value={pin}
                  onChange={(event) => setPin(event.target.value)}
                  required
                />
              </div>

              <label
                htmlFor="live-risk-ack"
                className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[10px] leading-relaxed text-amber-100"
              >
                <Checkbox
                  id="live-risk-ack"
                  checked={acknowledged}
                  onCheckedChange={(value) => setAcknowledged(value === true)}
                  className="mt-0.5 border-amber-400/60"
                />
                <span>
                  I understand this vault places <strong>real orders with real capital</strong>,
                  that the kill switch and ADR-009 stops are not a guarantee against loss, and
                  that this session expires in 30 minutes.
                </span>
              </label>

              {error ? (
                <p className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-[11px] text-destructive">
                  {error}
                </p>
              ) : null}

              <Button
                type="submit"
                className="w-full bg-rose-600 text-rose-50 hover:bg-rose-500"
                disabled={busy || !acknowledged}
              >
                <KeyRound className="mr-1 size-3.5" />
                {busy ? 'Verifying three factors…' : 'Unlock live sovereign vault'}
              </Button>
            </form>

            <div className="mt-5 flex items-center justify-between text-[11px] text-muted-foreground">
              <button type="button" className="underline" onClick={() => onDone('/login')}>
                Back to gateway
              </button>
              <button
                type="button"
                className="underline text-cyan-300"
                onClick={() => onDone('/paper/login')}
              >
                Paper sandbox →
              </button>
            </div>
            <p className="mt-4 text-[10px] leading-relaxed text-muted-foreground">
              scrypt password digest + PBKDF2-HMAC-SHA256 PIN digest, both verified in Ring 0 in
              constant time. A missing or invalid PIN is always <code>403 LIVE_PIN_REJECTED</code>.
            </p>
          </div>
        </div>
      </div>
    </PortalBackdrop>
  )
}

/** Re-exported for callers that expect the original first-run screen name. */
export { SetupPage, LoginPage, LiveUnlockPage } from '@/pages/auth'

