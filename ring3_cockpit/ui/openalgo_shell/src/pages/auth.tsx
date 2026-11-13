import { type FormEvent, useState } from 'react'
import { KeyRound, Lock, ShieldCheck, Sparkles } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { authApi, type AuthSession, type AuthStatus } from '@/lib/auth'

interface AuthScreenProps {
  status: AuthStatus | null
  onAuthenticated: (session: AuthSession) => Promise<void>
  onDone: (path: string) => void
}

function AuthShell({
  accent,
  icon,
  eyebrow,
  title,
  children,
}: {
  accent: string
  icon: React.ReactNode
  eyebrow: string
  title: string
  children: React.ReactNode
}) {
  return (
    <div className="dark">
      <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-background px-4 py-12 text-foreground">
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0"
          style={{ background: accent }}
        />
        <div className="relative z-10 w-full max-w-md rounded-2xl border border-border/60 bg-card/80 p-7 shadow-2xl backdrop-blur">
          <div className="mb-6 flex items-center gap-3">
            <span className="flex size-10 items-center justify-center rounded-xl bg-primary/15 text-primary">
              {icon}
            </span>
            <div>
              <p className="text-[10px] font-semibold uppercase tracking-[0.2em] text-muted-foreground">
                {eyebrow}
              </p>
              <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
            </div>
          </div>
          {children}
          <p className="mt-6 text-[10px] leading-relaxed text-muted-foreground">
            Ring 0 sovereign identity · salted scrypt / PBKDF2-HMAC-SHA256 · no plaintext
            credential ever leaves the encrypted key vault.
          </p>
        </div>
      </div>
    </div>
  )
}

export function SetupPage({ status, onAuthenticated, onDone }: AuthScreenProps) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [livePin, setLivePin] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const alreadyInitialized = Boolean(status?.initialized)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const session = await authApi.setup({
        username,
        password,
        live_pin: livePin,
      })
      await onAuthenticated(session)
      onDone('/paper/dashboard')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'setup failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthShell
      accent="radial-gradient(50rem 28rem at 18% 0%, rgba(16,185,129,0.18), transparent 70%), radial-gradient(40rem 22rem at 82% 12%, rgba(6,182,212,0.16), transparent 70%)"
      icon={<Sparkles className="size-5" />}
      eyebrow="First run · sovereign operator"
      title="Create the operator identity"
    >
      {alreadyInitialized ? (
        <div className="mb-5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200">
          An operator is already registered. Use{' '}
          <button
            type="button"
            className="underline"
            onClick={() => onDone('/login')}
          >
            sign in
          </button>{' '}
          or perform a factory reset from the cockpit.
        </div>
      ) : null}
      <form className="space-y-4" onSubmit={submit}>
        <div className="space-y-1.5">
          <Label htmlFor="setup-username">Operator handle</Label>
          <Input
            id="setup-username"
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            placeholder="sovereign"
            required
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="setup-password">Paper password (min 8 chars)</Label>
          <Input
            id="setup-password"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="setup-pin">Live elevation PIN (4–12 digits)</Label>
          <Input
            id="setup-pin"
            type="password"
            inputMode="numeric"
            pattern="[0-9]*"
            autoComplete="off"
            value={livePin}
            onChange={(event) => setLivePin(event.target.value)}
            required
          />
        </div>
        <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-[11px] text-emerald-200">
          <ShieldCheck className="mr-1.5 inline size-3.5" />
          Start Clean Slate (All Values = 0) — every silo, vault, order and counter
          begins at zero. Nothing is pre-funded, nothing is pre-traded.
        </div>
        {error ? (
          <p className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-[11px] text-destructive">
            {error}
          </p>
        ) : null}
        <Button type="submit" className="w-full" disabled={busy}>
          {busy ? 'Creating operator…' : 'Start Clean Slate'}
        </Button>
      </form>
    </AuthShell>
  )
}

export function LoginPage({ status, onAuthenticated, onDone }: AuthScreenProps) {
  const [username, setUsername] = useState(status?.username ?? '')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const session = await authApi.login({ username, password })
      await onAuthenticated(session)
      onDone('/paper/dashboard')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'login failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthShell
      accent="radial-gradient(50rem 28rem at 20% 0%, rgba(6,182,212,0.18), transparent 70%), radial-gradient(40rem 22rem at 80% 10%, rgba(16,185,129,0.14), transparent 70%)"
      icon={<KeyRound className="size-5" />}
      eyebrow="Paper sandbox session"
      title="Operator sign in"
    >
      <form className="space-y-4" onSubmit={submit}>
        <div className="space-y-1.5">
          <Label htmlFor="login-username">Operator handle</Label>
          <Input
            id="login-username"
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            required
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="login-password">Password</Label>
          <Input
            id="login-password"
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
        <Button type="submit" className="w-full" disabled={busy}>
          {busy ? 'Signing in…' : 'Enter Paper Sandbox'}
        </Button>
      </form>
      <div className="mt-4 flex items-center justify-between text-[11px] text-muted-foreground">
        <button type="button" className="underline" onClick={() => onDone('/setup')}>
          Create operator
        </button>
        <button
          type="button"
          className="underline"
          onClick={() => onDone('/live-unlock')}
        >
          Unlock live
        </button>
      </div>
    </AuthShell>
  )
}

export function LiveUnlockPage({ status, onAuthenticated, onDone }: AuthScreenProps) {
  const [pin, setPin] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const paperSession = status?.authenticated && status.tier === 'PAPER'

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const session = await authApi.elevate(pin)
      await onAuthenticated(session)
      onDone('/live/dashboard')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'elevation failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthShell
      accent="radial-gradient(50rem 28rem at 18% 0%, rgba(244,63,94,0.20), transparent 70%), radial-gradient(40rem 22rem at 82% 8%, rgba(245,158,11,0.16), transparent 70%)"
      icon={<Lock className="size-5" />}
      eyebrow="Live production · second factor"
      title="Live elevation PIN"
    >
      <div className="mb-5 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-[11px] text-rose-200">
        LIVE PRODUCTION is a separate workspace. Elevation expires after 30 minutes
        and every order path still runs the 98% conformal gate, the 22-gate
        pre-flight and the &lt;1 ms kill switch.
      </div>
      {paperSession ? null : (
        <div className="mb-5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200">
          A PAPER session is required first.{' '}
          <button type="button" className="underline" onClick={() => onDone('/login')}>
            Sign in
          </button>
          .
        </div>
      )}
      <form className="space-y-4" onSubmit={submit}>
        <div className="space-y-1.5">
          <Label htmlFor="live-pin">Live PIN</Label>
          <Input
            id="live-pin"
            type="password"
            inputMode="numeric"
            pattern="[0-9]*"
            value={pin}
            onChange={(event) => setPin(event.target.value)}
            required
          />
        </div>
        {error ? (
          <p className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-[11px] text-destructive">
            {error}
          </p>
        ) : null}
        <Button type="submit" className="w-full" disabled={busy || !paperSession}>
          {busy ? 'Verifying…' : 'Elevate to LIVE'}
        </Button>
      </form>
    </AuthShell>
  )
}
