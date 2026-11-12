import { useEffect, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Cpu,
  DollarSign,
  ExternalLink,
  Gauge,
  KeyRound,
  Moon,
  Network,
  Package,
  RefreshCw,
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
import { Progress } from '@/components/ui/progress'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Separator } from '@/components/ui/separator'
import { Switch } from '@/components/ui/switch'
import { postBroker, postBrokerCredentials, postExtensions, postLLM, postLLMProbe, fetchMetrics, fetchVaultStatus, metricsAliasUrl, metricsJsonUrl, metricsUrl, postVaultRotate } from '@/lib/api'
import { formatPct, formatUsd } from '@/lib/format'
import type {
  BrokerCredentialReport,
  BrokerCredentialSiloState,
  CockpitState,
  ExtensionsState,
  LLMProbeReport,
  LLMSlot,
  MetricsSnapshot,
  SiloState,
  VaultKey,
  VaultRotationReport,
  VaultStatus,
} from '@/types'

interface ControlRoomTabProps {
  state: CockpitState | null
  onSaved: () => Promise<void>
}

/** Constitutional token ceiling (`sec_013`): $86.40/day -> $3.10/day. */
const DAILY_TOKEN_BUDGET_USD = 3.1

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
      <ReleaseStatusPanel state={state} />
      <TelemetryMetricsPanel />

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
            <LLMProbeCard initial={asProbeReport(state?.llm_probe)} />
          </>
        ) : null}
        </div>
      </div>

      <BrokerCredentialVault state={state} onSaved={onSaved} />

      <VaultRotationPanel />

      <ExtensionsPanel
        extensions={state?.extensions ?? null}
        onSaved={onSaved}
      />
    </div>
  )
}

function ReleaseStatusPanel({ state }: { state: CockpitState | null }) {
  const silos = state ? Object.values(state.silos) : []
  const readyCount = silos.reduce((sum, silo) => sum + silo.ready_adapters.length, 0)
  const probeCount = silos.reduce((sum, silo) => sum + silo.adapters.length, 0)
  const allReady = probeCount > 0 && readyCount === probeCount

  const spend = state?.llm.daily_spend_usd ?? 0
  const cap = state?.llm.daily_spend_cap_usd ?? DAILY_TOKEN_BUDGET_USD
  const utilization = cap > 0 ? Math.min(100, (spend / cap) * 100) : 0
  const overBudget = spend > cap

  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <Card className="gap-3 border-border/70 bg-card/60 py-4 shadow-none">
        <CardHeader className="px-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Gauge className="size-4 text-primary" />
              Token budget
            </CardTitle>
            <Badge
              variant={overBudget ? 'destructive' : 'secondary'}
              className="font-mono text-[9px]"
            >
              {overBudget ? 'CAP BREACHED' : `${utilization.toFixed(1)}% used`}
            </Badge>
          </div>
          <p className="text-[11px] text-muted-foreground">
            The LLM never enters the microsecond loop. Paid cloud inference is metered
            per call and hard-capped at the constitutional daily ceiling.
          </p>
        </CardHeader>
        <CardContent className="space-y-3 px-4">
          <div className="flex items-end justify-between gap-3">
            <div>
              <div className="font-mono text-2xl font-semibold tabular-nums text-foreground">
                ${DAILY_TOKEN_BUDGET_USD.toFixed(2)}
                <span className="ml-1 text-[11px] font-normal text-muted-foreground">
                  / day ceiling
                </span>
              </div>
              <div className="mt-1 text-[10px] text-muted-foreground">
                from $86.40/day ·{' '}
                <span className="font-mono text-emerald-300">−96.4%</span> ·{' '}
                <span className="font-mono text-foreground">2,140×</span> faster ticks
              </div>
            </div>
            <div className="text-right">
              <div className="font-mono text-sm font-semibold tabular-nums">
                {formatUsd(spend, 2)}
              </div>
              <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
                spent today
              </div>
            </div>
          </div>

          <Progress
            value={utilization}
            className={`h-2 ${overBudget ? 'text-red-400' : ''}`}
          />

          <div className="grid grid-cols-2 gap-2 border-t border-border/60 pt-2">
            <div className="text-[10px] text-muted-foreground">
              System 1 ·{' '}
              <span className="font-mono text-foreground">
                {formatUsd(state?.llm.system1.daily_spend_cap_usd ?? 0, 2)}
              </span>{' '}
              cap
            </div>
            <div className="text-right text-[10px] text-muted-foreground">
              System 2 ·{' '}
              <span className="font-mono text-foreground">
                {formatUsd(state?.llm.system2.daily_spend_cap_usd ?? 0, 2)}
              </span>{' '}
              cap
            </div>
          </div>
        </CardContent>
      </Card>

      <Card className="gap-3 border-border/70 bg-card/60 py-4 shadow-none">
        <CardHeader className="px-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Network className="size-4 text-primary" />
              Venue adapter readiness
            </CardTitle>
            <Badge
              variant={allReady ? 'secondary' : 'destructive'}
              className="font-mono text-[9px] tracking-wider"
            >
              {probeCount === 0
                ? 'AWAITING PROBES'
                : `${readyCount} / ${probeCount} READY`}
            </Badge>
          </div>
          <p className="text-[11px] text-muted-foreground">
            Every silo holds its own encrypted adapters. Ring 3 reads readiness only — it
            never imports a broker module.
          </p>
        </CardHeader>
        <CardContent className="space-y-2 px-4">
          {silos.map((silo) => (
            <div
              key={silo.silo_id}
              className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border/60 bg-muted/20 px-2 py-1.5"
            >
              <div className="min-w-0">
                <div className="flex items-center gap-1.5 text-[11px] font-medium">
                  {silo.market}
                  <Badge variant="outline" className="font-mono text-[8px]">
                    {silo.silo_id}
                  </Badge>
                </div>
                <div className="mt-0.5 flex flex-wrap gap-1">
                  {silo.adapters.map((adapter) => (
                    <span
                      key={adapter.name}
                      title={adapter.reason || adapter.status}
                      className={`rounded border px-1 py-0.5 font-mono text-[8px] ${
                        adapter.ready
                          ? 'border-emerald-500/40 text-emerald-300'
                          : 'border-border text-muted-foreground'
                      }`}
                    >
                      {adapter.name}
                    </span>
                  ))}
                </div>
              </div>
              <span
                className={`font-mono text-[10px] tabular-nums ${
                  silo.ready_adapters.length === silo.adapters.length
                    ? 'text-emerald-300'
                    : 'text-amber-300'
                }`}
              >
                {silo.ready_adapters.length}/{silo.adapters.length}
              </span>
            </div>
          ))}
          {silos.length === 0 ? (
            <div className="rounded-md border border-dashed border-border/70 px-2 py-4 text-center text-[10px] text-muted-foreground">
              Waiting for the silo registry probe…
            </div>
          ) : null}
          <div className="flex items-center gap-1.5 border-t border-border/60 pt-2 text-[10px] text-muted-foreground">
            <ShieldCheck className="size-3" />
            {allReady
              ? 'All harvested venue adapters import cleanly across the four silos.'
              : 'Unready adapters are quarantined; the affected silo cannot arm live.'}
          </div>
        </CardContent>
      </Card>
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
  type = 'text',
  placeholder,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  hint?: string
  type?: 'text' | 'password'
  placeholder?: string
}) {
  return (
    <div className="space-y-1">
      <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </Label>
      <Input
        value={value}
        type={type}
        placeholder={placeholder}
        inputMode={type === 'password' ? 'text' : 'decimal'}
        autoComplete={type === 'password' ? 'off' : undefined}
        onChange={(event) => onChange(event.target.value)}
        className="h-7 font-mono text-xs tabular-nums"
      />
      {hint ? <div className="text-[9px] text-muted-foreground">{hint}</div> : null}
    </div>
  )
}

/* ------------------------------------------------------------------ *
 * Phase 3 Step 2 · write-only broker testnet credential vault
 * ------------------------------------------------------------------ */

function BrokerCredentialVault({
  state,
  onSaved,
}: {
  state: CockpitState | null
  onSaved: () => Promise<void>
}) {
  const credentialState = state?.broker_credentials
  const silos: BrokerCredentialSiloState[] = Object.values(credentialState?.silos ?? {})
  const configured = silos.reduce((sum, silo) => sum + (silo.configured ?? 0), 0)
  const supported = credentialState?.venues_supported?.length ?? 0

  return (
    <Card className="gap-3 border-border/70 bg-card/60 py-4 shadow-none">
      <CardHeader className="px-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            <ShieldCheck className="size-4 text-primary" />
            Broker testnet credential vault
          </CardTitle>
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline" className="font-mono text-[9px]">
              WRITE-ONLY · {supported} VENUES
            </Badge>
            <Badge
              variant={configured > 0 ? 'secondary' : 'outline'}
              className="font-mono text-[9px]"
            >
              {configured} CONFIGURED
            </Badge>
            <Badge variant="outline" className="font-mono text-[9px]">
              Ed25519 + Fernet · Ring 0
            </Badge>
          </div>
        </div>
        <p className="text-[11px] text-muted-foreground">
          Secrets are encrypted inside <span className="font-mono">ring0_vault/silos/key_vault</span>{' '}
          and never returned. Each save runs a deterministic venue-native payload
          preflight, signs a SHA-256 digest with Ed25519 and proves the Fernet
          envelope round-trips — no socket is opened and no order is placed.
        </p>
      </CardHeader>
      <CardContent className="grid gap-3 px-4 xl:grid-cols-2">
        {silos.map((silo) => (
          <VenueCredentialCard
            key={`${silo.silo_id}-${silo.configured}`}
            silo={silo}
            onSaved={onSaved}
          />
        ))}
        {silos.length === 0 ? (
          <div className="rounded-md border border-dashed border-border/60 bg-muted/10 px-3 py-6 text-center text-xs text-muted-foreground xl:col-span-2">
            Waiting for the Ring 0 credential inventory…
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}

function VenueCredentialCard({
  silo,
  onSaved,
}: {
  silo: BrokerCredentialSiloState
  onSaved: () => Promise<void>
}) {
  const [venue, setVenue] = useState(silo.venues[0]?.venue ?? '')
  const [apiKey, setApiKey] = useState('')
  const [apiSecret, setApiSecret] = useState('')
  const [sandbox, setSandbox] = useState(true)
  const [busy, setBusy] = useState(false)
  const [report, setReport] = useState<BrokerCredentialReport | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)

  const existing = silo.venues.find((entry) => entry.venue === venue)
  const configured = Boolean(existing?.credential_configured)
  const masked = report?.api_key_masked ?? existing?.api_key_masked ?? null

  const submit = async () => {
    setBusy(true)
    setMessage(null)
    setFailed(false)
    try {
      const next = await postBrokerCredentials({
        silo: silo.silo_id,
        venue,
        api_key: apiKey.trim(),
        api_secret: apiSecret.trim(),
        sandbox,
      })
      setReport(next)
      setApiKey('')
      setApiSecret('')
      setMessage(
        `Encrypted in Ring 0 · ${next.api_key_masked} · dry-run signed over ${next.dry_run.payload_field_count} fields in ${next.dry_run.elapsed_us.toFixed(0)} µs`
      )
      await onSaved()
    } catch (cause) {
      setFailed(true)
      setMessage(cause instanceof Error ? cause.message : 'credential handshake failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-2 rounded-md border border-border/60 bg-background/40 px-3 py-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          {silo.market}
          <span className="ml-1.5 font-mono normal-case text-foreground/70">
            {silo.silo_id}
          </span>
        </span>
        <div className="flex items-center gap-1.5">
          <Badge
            variant={configured ? 'secondary' : 'outline'}
            className="font-mono text-[9px]"
          >
            {configured ? `CONFIGURED ${masked ?? ''}` : 'NOT CONFIGURED'}
          </Badge>
          {report?.dry_run_verified ? (
            <Badge
              variant="secondary"
              className="border-emerald-500/40 font-mono text-[9px] text-emerald-300"
            >
              DRY-RUN VERIFIED
            </Badge>
          ) : null}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <div className="col-span-2">
          <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">
            Venue
          </Label>
          <Select value={venue} onValueChange={setVenue}>
            <SelectTrigger className="h-7 text-xs">
              <SelectValue placeholder="select venue" />
            </SelectTrigger>
            <SelectContent>
              {silo.venues.map((entry) => (
                <SelectItem key={entry.venue} value={entry.venue} className="text-xs">
                  {entry.venue}
                  {entry.credential_configured ? ' · configured' : ''}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <Field
          label="API key"
          value={apiKey}
          onChange={setApiKey}
          type="password"
          placeholder={masked ?? 'write-only'}
        />
        <Field
          label="API secret"
          value={apiSecret}
          onChange={setApiSecret}
          type="password"
          placeholder="write-only"
        />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <label className="flex items-center gap-2 text-[10px] text-muted-foreground">
          <Switch checked={sandbox} onCheckedChange={setSandbox} aria-label="sandbox credential" />
          Sandbox / testnet host
        </label>
        <Button
          type="button"
          size="sm"
          onClick={() => void submit()}
          disabled={busy || !venue || !apiKey.trim() || !apiSecret.trim()}
          className="gap-1.5"
        >
          <KeyRound className="size-3.5" />
          {busy ? 'Signing…' : 'Encrypt + dry-run sign'}
        </Button>
      </div>

      {report ? (
        <div className="rounded-md border border-emerald-500/30 bg-emerald-500/[0.06] px-2 py-1 font-mono text-[9px] text-emerald-200">
          {report.dry_run.signature_alg} sig {report.dry_run.signature_hex.slice(0, 16)}… · schema{' '}
          {report.dry_run.schema_ok ? 'OK' : 'FAIL'} · fernet{' '}
          {report.dry_run.fernet_roundtrip_ok ? 'OK' : 'FAIL'} · socket{' '}
          {report.dry_run.socket_opened ? 'OPENED' : 'CLOSED'}
        </div>
      ) : null}

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

/* ------------------------------------------------------------------ *
 * Phase 3 Step 2 · live local Ollama / LLM probe (<250 ms)
 * ------------------------------------------------------------------ */

/** The gateway returns `{}` before the first probe; narrow it honestly. */
function asProbeReport(
  value: CockpitState['llm_probe'] | undefined
): LLMProbeReport | null {
  if (!value) return null
  const candidate = value as Partial<LLMProbeReport>
  return typeof candidate.status === 'string' && candidate.system1
    ? (value as LLMProbeReport)
    : null
}

function LLMProbeCard({ initial }: { initial: LLMProbeReport | null }) {
  const [report, setReport] = useState<LLMProbeReport | null>(initial)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)

  const probe = async () => {
    setBusy(true)
    setMessage(null)
    setFailed(false)
    try {
      const next = await postLLMProbe()
      setReport(next)
      setMessage(
        next.status === 'LOCAL_OLLAMA_LIVE'
          ? `Local daemon answered in ${next.system1.latency_ms.toFixed(1)} ms · ${next.model_count} models`
          : `Fallback engaged after ${next.system1.latency_ms.toFixed(1)} ms · zero tokens spent`
      )
    } catch (cause) {
      setFailed(true)
      setMessage(cause instanceof Error ? cause.message : 'LLM probe failed')
    } finally {
      setBusy(false)
    }
  }

  const live = report?.status === 'LOCAL_OLLAMA_LIVE'

  return (
    <Card className="gap-2 border-border/70 bg-card/60 py-3 shadow-none">
      <CardContent className="space-y-2 px-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-[11px]">
            <Gauge className="size-3.5 text-muted-foreground" />
            <span className="text-muted-foreground">Live local LLM probe</span>
            <Badge
              variant={live ? 'secondary' : 'outline'}
              className={`font-mono text-[9px] ${
                live ? 'border-emerald-500/40 text-emerald-300' : 'text-amber-300'
              }`}
            >
              {report?.status ?? 'NOT PROBED'}
            </Badge>
          </div>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => void probe()}
            disabled={busy}
            className="gap-1.5"
          >
            <RefreshCw className={`size-3.5 ${busy ? 'animate-spin' : ''}`} />
            {busy ? 'Probing…' : 'Probe LLMs (<250ms)'}
          </Button>
        </div>

        {report ? (
          <div className="grid gap-2 sm:grid-cols-2">
            <div className="rounded-md border border-border/60 bg-background/40 px-2 py-1">
              <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
                System 1 · {report.system1.provider}
              </div>
              <div className="font-mono text-[10px] tabular-nums">
                {report.system1.latency_ms.toFixed(2)} ms ·{' '}
                <span className={report.system1.within_budget ? 'text-emerald-300' : 'text-amber-300'}>
                  {report.system1.within_budget ? 'in budget' : 'over cap'}
                </span>
              </div>
              <div className="truncate font-mono text-[9px] text-muted-foreground">
                {report.model_count > 0
                  ? report.models.map((model) => model.name).join(' · ')
                  : report.system1.error || 'no models discovered'}
              </div>
            </div>
            <div className="rounded-md border border-border/60 bg-background/40 px-2 py-1">
              <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
                System 2 · {report.system2?.provider ?? 'not configured'}
              </div>
              <div className="font-mono text-[10px] tabular-nums">
                {report.system2
                  ? `${report.system2.latency_ms.toFixed(2)} ms · ${
                      report.system2.reachable ? 'reachable' : 'unreachable'
                    }`
                  : '—'}
              </div>
              <div className="font-mono text-[9px] text-muted-foreground">
                hard cap {report.hard_timeout_ms.toFixed(0)} ms · circuit{' '}
                {report.circuit_breaker.tripped ? 'TRIPPED' : 'CLOSED'}
              </div>
            </div>
          </div>
        ) : null}

        {message ? (
          <div
            className={`text-[10px] ${
              failed ? 'text-red-300' : 'text-emerald-300'
            }`}
          >
            {message}
          </div>
        ) : null}

        <div className="font-mono text-[9px] text-muted-foreground">
          LOCAL_OLLAMA_LIVE · DETERMINISTIC_INSTINCT_FALLBACK · zero tokens on
          fallback
        </div>
      </CardContent>
    </Card>
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

/* ------------------------------------------------------------------ *
 * Phase 3 Step 4 · live memory / telemetry chip + Prometheus links
 * ------------------------------------------------------------------ */

function TelemetryMetricsPanel() {
  const [metrics, setMetrics] = useState<MetricsSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const next = await fetchMetrics()
        if (!cancelled) {
          setMetrics(next)
          setError(null)
        }
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof Error ? err.message : 'metrics endpoint unavailable'
          )
        }
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), 5000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [])

  const p99Ms = (metrics?.tick_latency.quantiles_ns.p99 ?? 0) / 1_000_000
  const p99WithinBudget = metrics?.tick_latency.p99_within_budget ?? false
  const pending = metrics?.outbox.pending ?? 0
  const healthy = Boolean(metrics) && p99WithinBudget && pending === 0 && !error

  const chips: { label: string; value: string; tone?: 'ok' | 'warn' }[] = [
    {
      label: 'Gateway RSS',
      value:
        metrics?.process.rss_mb != null
          ? `${metrics.process.rss_mb.toFixed(1)} MB`
          : 'n/a',
    },
    {
      label: 'Python heap',
      value:
        metrics?.process.python_heap_mb != null
          ? `${metrics.process.python_heap_mb.toFixed(2)} MB`
          : 'off',
    },
    {
      label: 'Tick p99',
      value: `${p99Ms.toFixed(3)} ms`,
      tone: p99WithinBudget ? 'ok' : 'warn',
    },
    { label: 'Armed leases', value: String(metrics?.leases.active ?? 0) },
    {
      label: 'Outbox pending',
      value: String(pending),
      tone: pending === 0 ? 'ok' : 'warn',
    },
    {
      label: 'Q98 bound',
      value: `${(metrics?.conformal.q98_bps ?? 0).toFixed(1)} bps`,
    },
    {
      label: 'Token spend',
      value: `${formatUsd(metrics?.tokens.spend_usd ?? 0)} / ${formatUsd(
        metrics?.tokens.cap_usd ?? DAILY_TOKEN_BUDGET_USD
      )}`,
    },
    { label: 'Ticks', value: String(metrics?.ring1.ticks ?? 0) },
  ]

  return (
    <Card className="gap-2 border-border/70 bg-card/60 py-3 shadow-none">
      <CardContent className="space-y-3 px-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            <Activity className="size-3.5 text-primary" />
            Prometheus / OpenTelemetry telemetry
          </span>
          <span
            className={`flex items-center gap-1.5 rounded-md px-2 py-1 text-[10px] ${
              healthy
                ? 'border border-emerald-500/30 bg-emerald-500/10 text-emerald-200'
                : 'border border-amber-500/30 bg-amber-500/10 text-amber-200'
            }`}
          >
            {healthy ? (
              <CheckCircle2 className="size-3" />
            ) : (
              <AlertTriangle className="size-3" />
            )}
            {error ? 'SCRAPE ERROR' : healthy ? 'TELEMETRY LIVE' : 'DEGRADED'}
          </span>
        </div>

        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {chips.map((chip) => (
            <div
              key={chip.label}
              className="rounded-md border border-border/60 bg-background/40 px-2 py-1.5"
            >
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                {chip.label}
              </div>
              <div
                className={`font-mono text-xs tabular-nums ${
                  chip.tone === 'warn'
                    ? 'text-amber-300'
                    : chip.tone === 'ok'
                      ? 'text-emerald-300'
                      : 'text-foreground'
                }`}
              >
                {chip.value}
              </div>
            </div>
          ))}
        </div>

        <div className="flex flex-wrap items-center gap-3 text-[10px] text-muted-foreground">
          <a
            className="flex items-center gap-1 font-mono text-primary hover:underline"
            href={metricsUrl()}
            target="_blank"
            rel="noreferrer"
          >
            <ExternalLink className="size-3" />
            {metrics?.exposition.prometheus ?? '/api/v2/metrics'}
          </a>
          <a
            className="font-mono text-primary hover:underline"
            href={metricsAliasUrl()}
            target="_blank"
            rel="noreferrer"
          >
            /metrics
          </a>
          <a
            className="font-mono text-primary hover:underline"
            href={metricsJsonUrl()}
            target="_blank"
            rel="noreferrer"
          >
            /api/v2/metrics/json
          </a>
          <span>
            scrape {((metrics?.scrape_duration_seconds ?? 0) * 1000).toFixed(2)} ms
          </span>
        </div>

        {error ? <p className="text-[10px] text-red-300">{error}</p> : null}
      </CardContent>
    </Card>
  )
}

/* ------------------------------------------------------------------ *
 * Phase 3 Step 5 · Ring 0 zero-downtime key rotation + OS tuning
 * ------------------------------------------------------------------ */

function VaultRotationPanel() {
  const [status, setStatus] = useState<VaultStatus | null>(null)
  const [report, setReport] = useState<VaultRotationReport | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)

  const load = async () => {
    try {
      setStatus(await fetchVaultStatus())
    } catch (cause) {
      setFailed(true)
      setMessage(
        cause instanceof Error ? cause.message : 'vault status unavailable'
      )
    }
  }

  useEffect(() => {
    void load()
  }, [])

  const rotate = async () => {
    setBusy(true)
    setMessage(null)
    setFailed(false)
    try {
      const next = await postVaultRotate()
      setReport(next)
      setMessage(
        `Key v${next.key_version} live · ${next.reencrypted_llm_keys_count} LLM key(s) and ${next.reencrypted_broker_venues_count} venue(s) re-encrypted · zero restart`
      )
      await load()
    } catch (cause) {
      setFailed(true)
      setMessage(cause instanceof Error ? cause.message : 'key rotation failed')
    } finally {
      setBusy(false)
    }
  }

  const tuning = report?.os_tuning ?? status?.os_tuning ?? null
  const archived = report?.archived_public_keys ?? status?.archived_public_keys ?? []
  const tuningTone =
    tuning && tuning.error_count > 0
      ? 'red'
      : tuning && tuning.recommended > 0
        ? 'amber'
        : 'emerald'

  return (
    <Card className="gap-3 border-border/70 bg-card/60 py-4 shadow-none">
      <CardHeader className="px-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            <KeyRound className="size-4 text-primary" />
            Ring 0 vault key rotation
          </CardTitle>
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline" className="font-mono text-[9px]">
              KEY v{report?.key_version ?? status?.key_version ?? 1}
            </Badge>
            <Badge
              variant="outline"
              className="font-mono text-[9px]"
              title={
                tuning
                  ? `${tuning.platform} · ${tuning.profile} · ${tuning.applied} applied / ${tuning.recommended} recommended`
                  : 'OS tuning audit not loaded'
              }
            >
              <Cpu className="mr-1 size-3" />
              OS LOW-LATENCY{' '}
              {tuning
                ? `${tuning.applied} APPLIED / ${tuning.recommended} RECOMMENDED`
                : 'AUDIT PENDING'}
            </Badge>
            <Badge
              variant={tuningTone === 'emerald' ? 'secondary' : 'outline'}
              className={`font-mono text-[9px] ${
                tuningTone === 'red'
                  ? 'border-red-500/40 text-red-300'
                  : tuningTone === 'amber'
                    ? 'border-amber-500/40 text-amber-300'
                    : 'border-emerald-500/40 text-emerald-300'
              }`}
            >
              {tuning ? (tuning.ok ? 'PROBE OK' : 'PROBE ERROR') : 'NO PROBE'}
            </Badge>
          </div>
        </div>
        <p className="text-[11px] text-muted-foreground">
          Rotates the Fernet master keys and the Ed25519 order-signing keypair in
          place. Stored LLM and broker credentials are re-encrypted atomically, the
          previous public key is archived read-only so historical signed orders still
          verify, and no worker restarts. Responses carry counts and fingerprints
          only — never a key or a secret.
        </p>
      </CardHeader>
      <CardContent className="space-y-3 px-4">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <div className="rounded-md border border-border/60 bg-background/40 px-2 py-1.5">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              Ed25519 key
            </div>
            <div className="font-mono text-xs text-foreground">
              {report?.ed25519_public_fingerprint ??
                status?.ed25519_public_fingerprint ??
                '—'}
            </div>
          </div>
          <div className="rounded-md border border-border/60 bg-background/40 px-2 py-1.5">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              LLM keys
            </div>
            <div className="font-mono text-xs tabular-nums">
              {report?.reencrypted_llm_keys_count ??
                status?.llm_keys_stored ??
                0}
            </div>
          </div>
          <div className="rounded-md border border-border/60 bg-background/40 px-2 py-1.5">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              Broker venues
            </div>
            <div className="font-mono text-xs tabular-nums">
              {report?.reencrypted_broker_venues_count ??
                status?.broker_venues_stored ??
                0}
            </div>
          </div>
          <div className="rounded-md border border-border/60 bg-background/40 px-2 py-1.5">
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              Archived keys
            </div>
            <div className="font-mono text-xs tabular-nums">
              {archived.length} · read-only
            </div>
          </div>
        </div>

        {tuning ? (
          <div className="flex flex-wrap items-center gap-2 text-[10px] text-muted-foreground">
            <span className="font-mono">
              {tuning.platform} · {tuning.profile}
            </span>
            <span className="font-mono">
              TCP_NODELAY={tuning.socket_tcp_nodelay}
            </span>
            <span className="font-mono">
              transports {tuning.transports_ok ? 'OK' : 'GAP'}
            </span>
            <span className="font-mono">
              errors {tuning.error_count}
            </span>
            <span className="font-mono">run: python scripts/tune_os_performance.py --check</span>
          </div>
        ) : null}

        {report ? (
          <div className="flex flex-wrap gap-2 text-[9px]">
            {[
              ['ZERO LOSS', report.credentials_zero_loss],
              ['DRY-RUN VERIFIED', report.dry_run_verified],
              ['MASKS STABLE', report.masks_unchanged],
              ['HISTORY VERIFIES', report.historical_signature_verified],
              ['ZERO DOWNTIME', report.zero_downtime],
            ].map(([label, ok]) => (
              <span
                key={String(label)}
                className={`rounded border px-1.5 py-0.5 font-mono ${
                  ok
                    ? 'border-emerald-500/40 text-emerald-300'
                    : 'border-red-500/40 text-red-300'
                }`}
              >
                {String(label)}
              </span>
            ))}
          </div>
        ) : null}

        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-[10px] text-muted-foreground">
            {status?.last_rotation
              ? `last rotation v${status.last_rotation.key_version} by ${status.last_rotation.operator}`
              : 'no rotation recorded · initial key generation v1'}
            {status?.rotation_pending_recovery ? ' · RECOVERY PENDING' : ''}
          </span>
          <Button
            type="button"
            size="sm"
            onClick={() => void rotate()}
            disabled={busy}
            className="gap-1.5"
          >
            <RefreshCw className={`size-3.5 ${busy ? 'animate-spin' : ''}`} />
            {busy ? 'Rotating…' : 'Rotate Ring 0 Vault Keys'}
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
