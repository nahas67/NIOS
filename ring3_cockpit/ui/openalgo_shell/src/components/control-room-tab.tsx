import { useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  Cpu,
  DollarSign,
  KeyRound,
  Moon,
  Package,
  Save,
  ServerCog,
  ShieldCheck,
  SlidersHorizontal,
} from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Separator } from '@/components/ui/separator'
import { Switch } from '@/components/ui/switch'
import { postBroker, postExtensions, postLLM } from '@/lib/api'
import { formatPct, formatUsd } from '@/lib/format'
import type {
  CockpitState,
  ExtensionsState,
  LLMSlot,
  SiloState,
  VaultKey,
} from '@/types'

interface ControlRoomTabProps {
  state: CockpitState | null
  onSaved: () => Promise<void>
}

const VAULTS: { key: VaultKey; label: string; tag: string }[] = [
  { key: 'ReserveYield', label: 'Reserve Yield', tag: '15%' },
  { key: 'LongTermCompounder', label: 'Long-Term', tag: '35%' },
  { key: 'TacticalSwing', label: 'Tactical Swing', tag: 'swing' },
  { key: 'SpinalScalper', label: 'Spinal Scalper', tag: '50%' },
]

export function ControlRoomTab({ state, onSaved }: ControlRoomTabProps) {
  const silos = state ? Object.values(state.silos) : []
  return (
    <div className="space-y-4">
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]">
        <div className="space-y-4">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <ServerCog className="size-4 text-primary" />
            Broker silo hot-swap
          </h2>
          <p className="text-[11px] text-muted-foreground">
            Paper/Live mode, statutory cost floors, daily loss stops and in-silo vault
            allocation. Applied in memory; no worker restart.
          </p>
        </div>
        {silos.map((silo) => (
          <SiloControl
            key={`${silo.silo_id}-${silo.updated_at_ns}`}
            silo={silo}
            onSaved={onSaved}
          />
        ))}
        {silos.length === 0 ? (
          <Card className="border-dashed bg-transparent py-10 shadow-none">
            <CardContent className="text-center text-xs text-muted-foreground">
              Waiting for broker state…
            </CardContent>
          </Card>
        ) : null}
      </div>

      <div className="space-y-4">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <Cpu className="size-4 text-primary" />
            Multi-LLM switchboard
          </h2>
          <p className="text-[11px] text-muted-foreground">
            System 1 arms 60 s leases off-path; System 2 runs the investment committee.
            API keys are write-only and encrypted inside Ring 0.
          </p>
        </div>
        {state ? (
          <>
            <LLMControlCard
              key={`s1-${state.llm.system1.updated_at_ns}`}
              system={1}
              title="System 1 · Instinct"
              subtitle="<80 ms lease writer · 250 ms hard timeout"
              slot={state.llm.system1}
              onSaved={onSaved}
            />
            <LLMControlCard
              key={`s2-${state.llm.system2.updated_at_ns}`}
              system={2}
              title="System 2 · Committee"
              subtitle="Deep thesis · strict daily USD cap"
              slot={state.llm.system2}
              onSaved={onSaved}
            />
            <Card className="gap-2 border-border/70 bg-card/60 py-3 shadow-none">
              <CardContent className="flex items-center justify-between px-4 text-[11px]">
                <span className="flex items-center gap-2 text-muted-foreground">
                  <DollarSign className="size-3.5" />
                  Combined daily budget
                </span>
                <span className="font-mono tabular-nums">
                  {formatUsd(state.llm.daily_spend_usd, 2)} /{' '}
                  {formatUsd(state.llm.daily_spend_cap_usd, 2)}
                </span>
              </CardContent>
            </Card>
          </>
        ) : null}
        </div>
      </div>

      <ExtensionsPanel
        extensions={state?.extensions ?? null}
        onSaved={onSaved}
      />
    </div>
  )
}

function ExtensionsPanel({
  extensions,
  onSaved,
}: {
  extensions: ExtensionsState | null
  onSaved: () => Promise<void>
}) {
  const [busy, setBusy] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)

  if (!extensions) return null
  const { skills, plugins, mcp, forge } = extensions

  const run = async (target: 'skill' | 'plugin' | 'mcp_tool' | 'forge', name: string | undefined, enabled?: boolean) => {
    const token = name ? `${target}:${name}` : target
    setBusy(token)
    setMessage(null)
    setFailed(false)
    try {
      const response = await postExtensions({
        target,
        name,
        enabled,
        action: target === 'forge' ? 'run_audit' : undefined,
      })
      setMessage(
        target === 'forge'
          ? `Forge audit complete · promoted ${response.report?.promoted_count ?? 0}, retired ${response.report?.retired_count ?? 0}`
          : `${token} applied in memory · zero restart`
      )
      await onSaved()
    } catch (cause) {
      setFailed(true)
      setMessage(cause instanceof Error ? cause.message : 'extension update failed')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="space-y-3">
      <div>
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          <Package className="size-4 text-primary" />
          Extensions · Skills, Plugins &amp; MCP
        </h2>
        <p className="text-[11px] text-muted-foreground">
          Hot-swap factor packs, data loaders and domain skills with no worker restart.
          New strategies are born in the 30-fill shadow nursery; the Forge promotes
          only on Deflated Sharpe ≥ 1.50 and 30 winning shadow fills.
        </p>
      </div>

      <div className="grid gap-3 xl:grid-cols-3">
        <Card className="gap-2 border-border/70 bg-card/60 py-4 shadow-none">
          <CardHeader className="px-4">
            <CardTitle className="flex items-center justify-between text-sm">
              <span>Agent skills</span>
              <Badge variant="outline" className="font-mono text-[9px]">
                {skills.enabled_count}/{skills.count}
              </Badge>
            </CardTitle>
            <p className="text-[10px] text-muted-foreground">
              Progressive SKILL.md · {skills.token_overhead} frontmatter tokens ·{' '}
              {skills.bodies_loaded} bodies loaded
            </p>
          </CardHeader>
          <CardContent className="space-y-2 px-4">
            {skills.skills.map((skill) => (
              <div key={skill.name} className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate font-mono text-[10px]">{skill.name}</div>
                  <div className="text-[9px] text-muted-foreground">
                    {skill.silo} · {skill.body_loaded ? 'body loaded' : 'frontmatter only'}
                  </div>
                </div>
                <Switch
                  checked={skill.enabled}
                  disabled={busy !== null}
                  aria-label={`${skill.name} skill enabled`}
                  onCheckedChange={(checked) =>
                    void run('skill', skill.name, checked)
                  }
                />
              </div>
            ))}
          </CardContent>
        </Card>

        <Card className="gap-2 border-border/70 bg-card/60 py-4 shadow-none">
          <CardHeader className="px-4">
            <CardTitle className="flex items-center justify-between text-sm">
              <span>Hot-swap plugins</span>
              <Badge variant="outline" className="font-mono text-[9px]">
                {plugins.count}
              </Badge>
            </CardTitle>
            <p className="text-[10px] text-muted-foreground">
              Factor packs · loaders · broker silos. Shadow nursery: 0/{plugins.required_shadow_fills}.
            </p>
          </CardHeader>
          <CardContent className="space-y-2 px-4">
            {plugins.plugins.length === 0 ? (
              <div className="rounded-md border border-dashed border-border/70 px-2 py-3 text-center text-[10px] text-muted-foreground">
                No plugins registered. POST /api/v2/control/extensions after
                PluginRegistry.register().
              </div>
            ) : (
              plugins.plugins.map((plugin) => (
                <div key={plugin.name} className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate font-mono text-[10px]">{plugin.name}</div>
                    <div className="text-[9px] text-muted-foreground">
                      {plugin.kind} · {plugin.shadow_fills}/{plugin.required_shadow_fills}{' '}
                      fills · {plugin.graduated ? 'graduated' : 'shadow'}
                    </div>
                  </div>
                  <Badge
                    variant={plugin.graduated ? 'secondary' : 'outline'}
                    className="text-[9px]"
                  >
                    {plugin.status}
                  </Badge>
                </div>
              ))
            )}
          </CardContent>
        </Card>

        <Card className="gap-2 border-border/70 bg-card/60 py-4 shadow-none">
          <CardHeader className="px-4">
            <CardTitle className="flex items-center justify-between text-sm">
              <span>MCP tools</span>
              <Badge
                variant={mcp.air_gap.pass ? 'secondary' : 'destructive'}
                className="text-[9px]"
              >
                {mcp.air_gap.pass ? 'AIR-GAP PASS' : 'AIR-GAP FAIL'}
              </Badge>
            </CardTitle>
            <p className="text-[10px] text-muted-foreground">
              {mcp.count} read-only tools · 0 order signing · 0 key reads · HTTP/SSE/stdio
            </p>
          </CardHeader>
          <CardContent className="space-y-1.5 px-4">
            {mcp.tools.map((tool) => (
              <div key={tool.name} className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate font-mono text-[10px]">{tool.name}</div>
                  <div className="text-[9px] text-muted-foreground">
                    {tool.category} · {tool.read_only ? 'read-only' : 'safety halt'}
                  </div>
                </div>
                <Switch
                  checked={tool.enabled}
                  disabled={busy !== null}
                  aria-label={`${tool.name} enabled`}
                  onCheckedChange={(checked) =>
                    void run('mcp_tool', tool.name, checked)
                  }
                />
              </div>
            ))}
          </CardContent>
        </Card>
      </div>

      <Card className="gap-2 border-border/70 bg-card/60 py-4 shadow-none">
        <CardContent className="flex flex-wrap items-center justify-between gap-3 px-4">
          <div className="flex items-center gap-3">
            <Moon className="size-4 text-primary" />
            <div>
              <div className="text-xs font-semibold">
                Nocturnal Alpha Forge · {forge.schedule_utc} UTC
              </div>
              <div className="text-[10px] text-muted-foreground">
                Champions {forge.champion_count} · challengers {forge.challenger_count} ·{' '}
                retire &lt; {forge.thresholds.retirement_floor_bps.toFixed(2)} bps · promote
                DSR ≥ {forge.thresholds.promotion_dsr_floor.toFixed(2)} +{' '}
                {forge.thresholds.min_winning_shadow_fills} winning fills
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[10px] text-muted-foreground">
              Last: {forge.last_report ? `${forge.last_report.genomes_evaluated} genomes · ${forge.last_report.promoted_count} promoted` : 'not run'}
            </span>
            <Button
              type="button"
              size="sm"
              disabled={busy !== null}
              onClick={() => void run('forge', undefined)}
              className="gap-1.5"
            >
              <ShieldCheck className="size-3.5" />
              {busy === 'forge' ? 'Auditing…' : 'Run alpha audit'}
            </Button>
          </div>
        </CardContent>
      </Card>

      {message ? (
        <div
          className={`flex items-center gap-1.5 rounded-md px-2 py-1 text-[10px] ${
            failed
              ? 'border border-red-500/30 bg-red-500/10 text-red-200'
              : 'border border-emerald-500/30 bg-emerald-500/10 text-emerald-200'
          }`}
        >
          {failed ? <AlertTriangle className="size-3" /> : <CheckCircle2 className="size-3" />}
          {message}
        </div>
      ) : null}
    </div>
  )
}

function SiloControl({ silo, onSaved }: { silo: SiloState; onSaved: () => Promise<void> }) {
  const [mode, setMode] = useState<'PAPER' | 'LIVE'>(silo.mode)
  const [maker, setMaker] = useState(String(silo.cost_floor.maker_fee_bps))
  const [taker, setTaker] = useState(String(silo.cost_floor.taker_fee_bps))
  const [tax, setTax] = useState(String(silo.cost_floor.statutory_tax_bps))
  const [spread, setSpread] = useState(String(silo.cost_floor.spread_bps))
  const [lossUsd, setLossUsd] = useState(
    (silo.max_daily_loss_micros / 1_000_000).toFixed(2)
  )
  const [allocation, setAllocation] = useState<Record<VaultKey, number>>({
    ReserveYield: (silo.allocation.ReserveYield ?? 0.15) * 100,
    LongTermCompounder: (silo.allocation.LongTermCompounder ?? 0.35) * 100,
    TacticalSwing: (silo.allocation.TacticalSwing ?? 0) * 100,
    SpinalScalper: (silo.allocation.SpinalScalper ?? 0.5) * 100,
  })
  const [busy, setBusy] = useState(false)
  const [modeBusy, setModeBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)

  const total = Object.values(allocation).reduce((sum, value) => sum + value, 0)
  const feeStt = 2 * Number(maker || 0) + Number(tax || 0)

  const toggleMode = async (checked: boolean) => {
    const next: 'PAPER' | 'LIVE' = checked ? 'LIVE' : 'PAPER'
    setModeBusy(true)
    setMessage(null)
    setFailed(false)
    try {
      await postBroker({
        silo: silo.silo_id,
        mode: next,
        confirm_live: next === 'LIVE',
        operator: 'ring3.cockpit',
      })
      setMode(next)
      setMessage(next === 'LIVE' ? 'LIVE capital armed' : 'Silo returned to PAPER')
      await onSaved()
    } catch (cause) {
      setFailed(true)
      setMessage(cause instanceof Error ? cause.message : 'mode hot-swap failed')
    } finally {
      setModeBusy(false)
    }
  }

  const save = async () => {
    setBusy(true)
    setMessage(null)
    setFailed(false)
    try {
      const normalized = Object.fromEntries(
        Object.entries(allocation).map(([key, value]) => [
          key,
          total > 0 ? value / total : 0,
        ])
      ) as Record<string, number>
      await postBroker({
        silo: silo.silo_id,
        mode,
        maker_fee_bps: Number(maker),
        taker_fee_bps: Number(taker),
        statutory_tax_bps: Number(tax),
        spread_bps: Number(spread),
        max_daily_loss_micros: Math.round(Number(lossUsd) * 1_000_000),
        allocation: normalized,
        confirm_live: mode === 'LIVE',
        operator: 'ring3.cockpit',
      })
      setMessage('Applied in memory · zero restart')
      await onSaved()
    } catch (cause) {
      setFailed(true)
      setMessage(cause instanceof Error ? cause.message : 'hot-swap failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className="gap-3 border-border/70 bg-card/60 py-4 shadow-none">
      <CardHeader className="px-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            {silo.market}
            <Badge variant="outline" className="font-mono text-[9px]">
              {silo.silo_id}
            </Badge>
          </CardTitle>
          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Paper
            </span>
            <Switch
              checked={mode === 'LIVE'}
              disabled={modeBusy}
              onCheckedChange={(checked) => void toggleMode(checked)}
              aria-label={`${silo.market} paper or live mode`}
            />
            <span
              className={`text-[10px] uppercase tracking-wider ${
                mode === 'LIVE' ? 'text-red-300' : 'text-emerald-300'
              }`}
            >
              Live
            </span>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4 px-4">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Field label="Maker fee (bps)" value={maker} onChange={setMaker} />
          <Field label="Taker fee (bps)" value={taker} onChange={setTaker} />
          <Field label="STT + tax (bps)" value={tax} onChange={setTax} />
          <Field label="Spread floor (bps)" value={spread} onChange={setSpread} />
        </div>

        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="w-full max-w-[220px]">
            <Field
              label="Daily loss stop (USD)"
              value={lossUsd}
              onChange={setLossUsd}
              hint="locked automatically on breach · ADR-009 reduce-only"
            />
          </div>
          <div className="rounded-md border border-border/60 bg-muted/20 px-2 py-1 text-[10px] text-muted-foreground">
            Fee+STT floor{' '}
            <span className="font-mono text-foreground">{feeStt.toFixed(2)} bps</span>
          </div>
        </div>

        <Separator />

        <div>
          <div className="mb-2 flex items-center justify-between">
            <span className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
              <SlidersHorizontal className="size-3.5" />
              In-silo vault allocation
            </span>
            <span
              className={`font-mono text-[10px] ${
                Math.abs(total - 100) < 0.01 ? 'text-emerald-300' : 'text-amber-300'
              }`}
            >
              {total.toFixed(0)}% (normalized on save)
            </span>
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            {VAULTS.map((vault) => (
              <div key={vault.key} className="space-y-1">
                <div className="flex items-center justify-between text-[10px]">
                  <span className="text-muted-foreground">
                    {vault.label}{' '}
                    <span className="rounded bg-muted px-1 text-[8px] uppercase">
                      {vault.tag}
                    </span>
                  </span>
                  <span className="font-mono tabular-nums">
                    {allocation[vault.key].toFixed(0)}%
                  </span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={100}
                  step={1}
                  value={allocation[vault.key]}
                  onChange={(event) =>
                    setAllocation((previous) => ({
                      ...previous,
                      [vault.key]: Number(event.target.value),
                    }))
                  }
                  className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-muted accent-primary"
                  aria-label={`${vault.label} allocation`}
                />
              </div>
            ))}
          </div>
        </div>

        <div className="flex items-center justify-between gap-3">
          <div className="text-[10px] text-muted-foreground">
            {silo.ready_adapters.length} adapters ready ·{' '}
            {silo.venue} · {formatPct(silo.allocation.ReserveYield ?? 0)} reserve
          </div>
          <Button
            type="button"
            size="sm"
            onClick={() => void save()}
            disabled={busy}
            className="gap-1.5"
          >
            <Save className="size-3.5" />
            {busy ? 'Applying…' : 'Apply hot-swap'}
          </Button>
        </div>
        {message ? (
          <div
            className={`flex items-center gap-1.5 rounded-md px-2 py-1 text-[10px] ${
              failed
                ? 'border border-red-500/30 bg-red-500/10 text-red-200'
                : 'border border-emerald-500/30 bg-emerald-500/10 text-emerald-200'
            }`}
          >
            {failed ? (
              <AlertTriangle className="size-3" />
            ) : (
              <CheckCircle2 className="size-3" />
            )}
            {message}
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}

function Field({
  label,
  value,
  onChange,
  hint,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  hint?: string
}) {
  return (
    <div className="space-y-1">
      <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </Label>
      <Input
        value={value}
        inputMode="decimal"
        onChange={(event) => onChange(event.target.value)}
        className="h-7 font-mono text-xs tabular-nums"
      />
      {hint ? <div className="text-[9px] text-muted-foreground">{hint}</div> : null}
    </div>
  )
}

const PROVIDERS: Record<1 | 2, { value: string; label: string }[]> = {
  1: [
    { value: 'ollama', label: 'Local Ollama' },
    { value: 'gemini', label: 'Gemini 2.5 Flash-Lite' },
    { value: 'groq', label: 'Groq' },
    { value: 'deterministic-local', label: 'Deterministic local' },
  ],
  2: [
    { value: 'deepseek', label: 'DeepSeek-R1' },
    { value: 'anthropic', label: 'Claude Sonnet / Opus' },
    { value: 'openrouter', label: 'OpenRouter' },
    { value: 'openai', label: 'OpenAI' },
  ],
}

function LLMControlCard({
  system,
  title,
  subtitle,
  slot,
  onSaved,
}: {
  system: 1 | 2
  title: string
  subtitle: string
  slot: LLMSlot
  onSaved: () => Promise<void>
}) {
  const [provider, setProvider] = useState(slot.provider)
  const [model, setModel] = useState(slot.model)
  const [baseUrl, setBaseUrl] = useState(slot.base_url)
  const [budget, setBudget] = useState(String(slot.daily_spend_cap_usd))
  const [timeoutMs, setTimeoutMs] = useState(String(slot.timeout_ms))
  const [apiKey, setApiKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)

  const save = async () => {
    setBusy(true)
    setMessage(null)
    setFailed(false)
    try {
      await postLLM({
        system,
        provider,
        model,
        base_url: baseUrl,
        daily_spend_cap_usd: Number(budget),
        timeout_ms: Number(timeoutMs),
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
      })
      setApiKey('')
      setMessage('Hot-swapped · key stored write-only in Ring 0')
      await onSaved()
    } catch (cause) {
      setFailed(true)
      setMessage(cause instanceof Error ? cause.message : 'switchboard update failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className="gap-3 border-border/70 bg-card/60 py-4 shadow-none">
      <CardHeader className="px-4">
        <div className="flex items-start justify-between gap-2">
          <div>
            <CardTitle className="flex items-center gap-2 text-sm">
              {title}
              <Badge
                variant={slot.circuit_open ? 'destructive' : 'outline'}
                className="text-[9px]"
              >
                {slot.circuit_open ? 'CIRCUIT OPEN' : `${slot.timeout_ms} ms timeout`}
              </Badge>
            </CardTitle>
            <p className="mt-1 text-[10px] text-muted-foreground">{subtitle}</p>
          </div>
          <Badge variant="secondary" className="font-mono text-[9px]">
            {formatUsd(slot.spend_usd, 2)} / {formatUsd(slot.daily_spend_cap_usd, 2)}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3 px-4">
        <div className="grid grid-cols-2 gap-2">
          <div className="space-y-1">
            <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Provider
            </Label>
            <Select value={provider} onValueChange={setProvider}>
              <SelectTrigger className="h-7 w-full text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PROVIDERS[system].map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Field label="Model" value={model} onChange={setModel} />
        </div>
        <Field label="Base URL" value={baseUrl} onChange={setBaseUrl} />
        <div className="grid grid-cols-2 gap-2">
          <Field label="Daily USD cap" value={budget} onChange={setBudget} />
          <Field label="Timeout (ms)" value={timeoutMs} onChange={setTimeoutMs} />
        </div>
        <div className="space-y-1">
          <div className="flex items-center justify-between text-[9px] text-muted-foreground">
            <span>Daily budget slider</span>
            <span className="font-mono text-foreground">{formatUsd(Number(budget) || 0, 2)}</span>
          </div>
          <input
            type="range"
            min={0}
            max={10}
            step={0.05}
            value={Number(budget) || 0}
            onChange={(event) => setBudget(event.target.value)}
            className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-muted accent-primary"
            aria-label={`${title} daily USD budget`}
          />
        </div>

        <div className="space-y-1">
          <Label className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-muted-foreground">
            <KeyRound className="size-3" />
            API key · write-only
          </Label>
          <div className="flex items-center gap-2">
            <Input
              type="password"
              value={apiKey}
              autoComplete="new-password"
              placeholder={slot.api_key_masked ?? 'paste key — never returned'}
              onChange={(event) => setApiKey(event.target.value)}
              className="h-7 flex-1 font-mono text-xs"
            />
            <Badge variant="outline" className="font-mono text-[9px]">
              {slot.api_key_masked ?? 'UNSET'}
            </Badge>
          </div>
          <div className="text-[9px] text-muted-foreground">
            Stored encrypted under ring0_vault/silos/key_vault; responses only ever
            expose the last four characters.
          </div>
        </div>

        <div className="flex items-center justify-between gap-2">
          <span className="text-[9px] text-muted-foreground">
            Circuit: {slot.circuit_open ? 'open' : 'closed'} · last{' '}
            {slot.last_latency_ms.toFixed(1)} ms
          </span>
          <Button
            type="button"
            size="sm"
            onClick={() => void save()}
            disabled={busy}
            className="gap-1.5"
          >
            <Save className="size-3.5" />
            {busy ? 'Saving…' : 'Save switchboard'}
          </Button>
        </div>
        {message ? (
          <div
            className={`flex items-center gap-1.5 rounded-md px-2 py-1 text-[10px] ${
              failed
                ? 'border border-red-500/30 bg-red-500/10 text-red-200'
                : 'border border-emerald-500/30 bg-emerald-500/10 text-emerald-200'
            }`}
          >
            {failed ? (
              <AlertTriangle className="size-3" />
            ) : (
              <CheckCircle2 className="size-3" />
            )}
            {message}
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}
