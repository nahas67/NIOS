import { Cpu, Gauge, ShieldCheck, Skull, Timer, Waves, Zap } from 'lucide-react'
import { useState } from 'react'

import { ActionFeedback, ActionPanel, ProofRow } from '@/components/operator-action'
import { PageShell, StatTile } from '@/components/page-shell'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { useAction } from '@/hooks/use-action'
import { usePageData } from '@/hooks/use-page-data'
import { fetchMetrics, postChaosDrill, postOsTune, postVoprCampaign } from '@/lib/api'
import { cell } from '@/lib/rows'
import type {
  ChaosDrillReceipt,
  CockpitState,
  OsTuneReceipt,
  VoprCampaignReceipt,
} from '@/types'

interface VerificationPageProps {
  state: CockpitState | null
}

export function VerificationPage({ state }: VerificationPageProps) {
  const metrics = usePageData(fetchMetrics)
  const proofs = asRecord(state?.formal_proofs)
  const suites = asRows(proofs.proofs)
  const vopr = asRecord(proofs.vopr)
  const tuning = asRecord(proofs.os_tuning)
  const backendTb = asRecord(proofs.tigerbeetle)
  const backendPg = asRecord(proofs.postgres)

  const [seed, setSeed] = useState('7')
  const [steps, setSteps] = useState('250')
  const [apply, setApply] = useState(true)
  const drill = useAction<ChaosDrillReceipt>()
  const campaign = useAction<VoprCampaignReceipt>()
  const osTune = useAction<OsTuneReceipt>()

  async function runDrill() {
    const receipt = await drill.run(() => postChaosDrill({ iterations: 120, hold_ms: 120, seed: 11 }))
    if (receipt) metrics.reload()
  }

  async function runVopr() {
    const receipt = await campaign.run(() =>
      postVoprCampaign({
        seed: Number.parseInt(seed, 10) || 0,
        steps: Number.parseInt(steps, 10) || 250,
      })
    )
    if (receipt) metrics.reload()
  }

  async function runOsTune() {
    const receipt = await osTune.run(() => postOsTune({ apply, ring: 'ring1' }))
    if (receipt) metrics.reload()
  }

  return (
    <PageShell
      title="Formal Proofs, Chaos & Telemetry"
      ring="RING 0"
      capability="PAPER CONTROL"
      subtitle="TLA+ breadth-first state exploration, the TigerBeetle VOPR fault campaign, SIGKILL chaos recovery and the live Prometheus exporter."
      actions={
        <Badge variant="outline" className="font-mono text-[10px]">
          {suites.length} PROOF SUITES
        </Badge>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          icon={ShieldCheck}
          label="TLA+ states explored"
          value={`${numberish(proofs.total_states ?? proofs.states_explored ?? 0)}`}
          hint={`${suites.length} suites · ${numberish(proofs.violations ?? 0)} violations`}
        />
        <StatTile
          icon={Zap}
          label="VOPR campaign"
          value={`${numberish(vopr.steps ?? vopr.operations ?? 0)}`}
          hint={`${numberish(vopr.faults_injected ?? 0)} faults injected`}
        />
        <StatTile
          icon={Gauge}
          label="Kill switch"
          value={`${numberish(proofs.kill_switch_ns ?? 0)} ns`}
          hint="Budget: < 1,000,000 ns for a global flatten"
        />
        <StatTile
          icon={Cpu}
          label="Backends"
          value={`${backendTb.available ? 'TB' : '—'} / ${backendPg.available ? 'PG' : '—'}`}
          hint={`TigerBeetle ${String(backendTb.version ?? 'n/a')} · PG ${String(backendPg.version ?? 'n/a')}`}
        />
      </div>

      <ActionPanel
        title="Run Chaos SIGKILL Recovery Drill"
        description="Kills a worker mid-outbox, replays the journal on reboot and proves no orphaned order, at-most-once execution, a lease swept to DISARMED and sum(debits) == sum(credits)."
      >
        <div className="flex flex-wrap items-center gap-3">
          <Button type="button" size="sm" disabled={drill.pending} onClick={runDrill}>
            <Skull className="mr-1 size-3.5" />
            Run Chaos SIGKILL Recovery Drill
          </Button>
          <span className="font-mono text-[10px] text-muted-foreground">
            deterministic seed 11 · 120 iterations · 120 ms hold
          </span>
        </div>

        <div className="mt-3">
          <ActionFeedback pending={drill.pending} error={drill.error}>
            {drill.receipt ? (
              <div className="rounded-lg border border-border/70 bg-background/40 p-3">
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <Badge
                    variant={drill.receipt.all_invariants_hold ? 'default' : 'destructive'}
                    className="font-mono text-[10px]"
                  >
                    {drill.receipt.status}
                  </Badge>
                  <span className="font-mono text-[10px] text-muted-foreground">
                    {drill.receipt.target} · pid {drill.receipt.pid} · rc{' '}
                    {String(drill.receipt.returncode)} · {drill.receipt.elapsed_ms.toFixed(1)} ms
                  </span>
                </div>
                <div className="grid gap-x-6 sm:grid-cols-2">
                  <ProofRow
                    label="No orphaned orders"
                    value={proofBadge(drill.receipt.invariants.no_orphaned_orders)}
                  />
                  <ProofRow
                    label="At-most-once execution"
                    value={proofBadge(drill.receipt.invariants.at_most_once_execution)}
                  />
                  <ProofRow
                    label="Lease swept to DISARMED"
                    value={proofBadge(drill.receipt.invariants.stale_lease_swept_to_disarmed)}
                  />
                  <ProofRow
                    label="Debit == credit"
                    value={proofBadge(drill.receipt.invariants.double_entry_conservation)}
                  />
                  <ProofRow
                    label="pending_after"
                    value={String(drill.receipt.proof.pending_after)}
                  />
                  <ProofRow
                    label="duplicate_transfer_ids"
                    value={String(drill.receipt.proof.duplicate_transfer_ids)}
                  />
                  <ProofRow
                    label="sum(debits) µ"
                    value={String(drill.receipt.proof.sum_debits_micros)}
                  />
                  <ProofRow
                    label="sum(credits) µ"
                    value={String(drill.receipt.proof.sum_credits_micros)}
                  />
                  <ProofRow
                    label="lease mode after reboot"
                    value={String(drill.receipt.lease_reboot.mode_after_reboot)}
                  />
                  <ProofRow
                    label="SIGKILL delivered"
                    value={drill.receipt.sigkill_delivered ? 'YES' : 'NO'}
                  />
                </div>
              </div>
            ) : !drill.pending ? (
              <p className="text-[11px] text-muted-foreground">
                No drill receipt yet. The drill is deterministic: the same seed always reproduces
                the same reboot verdict.
              </p>
            ) : null}
          </ActionFeedback>
        </div>
      </ActionPanel>

      <div className="grid gap-3 lg:grid-cols-2">
        <ActionPanel
          title="Run Seeded VOPR Fault Campaign"
          description="Deterministic fault injection across the double-entry ledger: duplicate acks, out-of-order acks, power loss and overdraft attempts."
        >
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <Label
                htmlFor="vopr-seed"
                className="text-[10px] uppercase tracking-wider text-muted-foreground"
              >
                Seed
              </Label>
              <Input
                id="vopr-seed"
                type="number"
                min={0}
                value={seed}
                onChange={(event) => setSeed(event.target.value)}
                className="h-8 w-24 font-mono text-xs"
              />
            </div>
            <div className="space-y-1">
              <Label
                htmlFor="vopr-steps"
                className="text-[10px] uppercase tracking-wider text-muted-foreground"
              >
                Steps
              </Label>
              <Input
                id="vopr-steps"
                type="number"
                min={10}
                max={20000}
                value={steps}
                onChange={(event) => setSteps(event.target.value)}
                className="h-8 w-28 font-mono text-xs"
              />
            </div>
            <Button type="button" size="sm" disabled={campaign.pending} onClick={runVopr}>
              <Waves className="mr-1 size-3.5" />
              Run Seeded VOPR Fault Campaign
            </Button>
          </div>

          <div className="mt-3">
            <ActionFeedback pending={campaign.pending} error={campaign.error}>
              {campaign.receipt ? (
                <div className="rounded-lg border border-border/70 bg-background/40 p-3">
                  <div className="mb-2 flex flex-wrap items-center gap-2">
                    <Badge
                      variant={campaign.receipt.all_invariants_hold ? 'default' : 'destructive'}
                      className="font-mono text-[10px]"
                    >
                      {campaign.receipt.status}
                    </Badge>
                    <span className="font-mono text-[10px] text-muted-foreground">
                      seed {campaign.receipt.seed} · {campaign.receipt.steps} steps ·{' '}
                      {campaign.receipt.duration_ms.toFixed(1)} ms
                    </span>
                  </div>
                  <div className="grid gap-x-6 sm:grid-cols-2">
                    <ProofRow
                      label="Injected faults"
                      value={String(campaign.receipt.injected_faults)}
                    />
                    <ProofRow
                      label="Power-loss cycles"
                      value={String(campaign.receipt.power_cycles)}
                    />
                    <ProofRow label="Overdrafts" value={String(campaign.receipt.overdrafts)} />
                    <ProofRow
                      label="Conservation violations"
                      value={String(campaign.receipt.conservation_violations)}
                    />
                    <ProofRow
                      label="Duplicate executions"
                      value={String(campaign.receipt.duplicate_executions)}
                    />
                    <ProofRow
                      label="at_most_once_ok"
                      value={proofBadge(campaign.receipt.at_most_once_ok)}
                    />
                  </div>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {Object.entries(campaign.receipt.fault_counts).map(([fault, count]) => (
                      <Badge
                        key={fault}
                        variant="outline"
                        className="font-mono text-[9px] text-muted-foreground"
                      >
                        {fault} {count}
                      </Badge>
                    ))}
                  </div>
                </div>
              ) : !campaign.pending ? (
                <p className="text-[11px] text-muted-foreground">
                  No campaign receipt yet. Same seed and step count always reproduce the same fault
                  histogram.
                </p>
              ) : null}
            </ActionFeedback>
          </div>
        </ActionPanel>

        <ActionPanel
          title="Apply & Audit OS Low-Latency Tuning"
          description="Probes a real loopback TCP pair for TCP_NODELAY, SO_KEEPALIVE and 1 MiB buffers, then reads back timer resolution, priority class and CPU affinity."
        >
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-2">
              <Switch id="os-apply" checked={apply} onCheckedChange={setApply} />
              <Label htmlFor="os-apply" className="text-[11px]">
                Apply what this process may change
              </Label>
            </div>
            <Button type="button" size="sm" disabled={osTune.pending} onClick={runOsTune}>
              <Timer className="mr-1 size-3.5" />
              Apply &amp; Audit OS Low-Latency Tuning
            </Button>
          </div>

          <div className="mt-3">
            <ActionFeedback pending={osTune.pending} error={osTune.error}>
              {osTune.receipt ? (
                <div className="rounded-lg border border-border/70 bg-background/40 p-3">
                  <div className="mb-2 flex flex-wrap items-center gap-2">
                    <Badge
                      variant={osTune.receipt.error_count === 0 ? 'default' : 'destructive'}
                      className="font-mono text-[10px]"
                    >
                      {osTune.receipt.status}
                    </Badge>
                    <span className="font-mono text-[10px] text-muted-foreground">
                      {osTune.receipt.platform} {osTune.receipt.platform_release} ·{' '}
                      {osTune.receipt.profile}
                    </span>
                  </div>
                  <div className="grid gap-x-6 sm:grid-cols-2">
                    <ProofRow label="Errors" value={String(osTune.receipt.error_count)} />
                    <ProofRow label="Applied rows" value={String(osTune.receipt.applied_count)} />
                    <ProofRow
                      label="Recommended rows"
                      value={String(osTune.receipt.recommended_count)}
                    />
                    <ProofRow
                      label="TCP_NODELAY"
                      value={String(osTune.receipt.socket_telemetry.tcp_nodelay)}
                    />
                    <ProofRow
                      label="SO_KEEPALIVE"
                      value={String(osTune.receipt.socket_telemetry.so_keepalive)}
                    />
                    <ProofRow
                      label="RX / TX buffer"
                      value={`${osTune.receipt.socket_telemetry.recv_buffer_bytes} / ${osTune.receipt.socket_telemetry.send_buffer_bytes}`}
                    />
                  </div>
                  <div className="mt-2 space-y-1">
                    {osTune.receipt.checks.map((check) => (
                      <div
                        key={check.id}
                        className="flex items-center justify-between gap-2 font-mono text-[10px]"
                      >
                        <span className="text-muted-foreground">{check.id}</span>
                        <span className="flex items-center gap-2">
                          <span className="max-w-72 truncate text-muted-foreground">
                            {check.detail}
                          </span>
                          <Badge variant="outline" className="font-mono text-[9px]">
                            {check.status}
                          </Badge>
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              ) : !osTune.pending ? (
                <p className="text-[11px] text-muted-foreground">
                  No audit receipt yet. A RECOMMENDED row prints its exact command; it is never
                  silently skipped.
                </p>
              ) : null}
            </ActionFeedback>
          </div>
        </ActionPanel>
      </div>

      <Card className="border-border/70 bg-card/60 shadow-none">
        <CardHeader className="px-4">
          <CardTitle className="text-sm">TLA+ BFS proof table</CardTitle>
          <p className="text-[11px] text-muted-foreground">
            KillSwitch · OrderLifecycle · Outbox · Reconciliation — every invariant is
            model-checked, never asserted.
          </p>
        </CardHeader>
        <CardContent className="space-y-2 px-4">
          {suites.length === 0 ? (
            <p className="rounded-lg border border-dashed border-border/70 px-3 py-6 text-center text-xs text-muted-foreground">
              No proof suites reported yet.
            </p>
          ) : (
            suites.map((row, index) => (
              <div
                key={String(row.name ?? row.spec ?? `suite-${index}`)}
                className="rounded-lg border border-border/50 bg-background/40 px-3 py-2"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-mono text-xs font-medium">
                    {String(row.name ?? row.spec ?? 'suite')}
                  </span>
                  <Badge
                    variant={row.passed === false ? 'destructive' : 'outline'}
                    className="font-mono text-[9px]"
                  >
                    {row.passed === false ? 'VIOLATED' : 'HOLDS'}
                  </Badge>
                </div>
                <KeyValues data={row} />
              </div>
            ))
          )}
        </CardContent>
      </Card>

      <div className="grid gap-3 lg:grid-cols-2">
        <Card className="border-border/70 bg-card/60 shadow-none">
          <CardHeader className="px-4">
            <CardTitle className="text-sm">VOPR fault campaign & chaos recovery</CardTitle>
          </CardHeader>
          <CardContent className="px-4">
            <KeyValues data={vopr} />
          </CardContent>
        </Card>

        <Card className="border-border/70 bg-card/60 shadow-none">
          <CardHeader className="px-4">
            <CardTitle className="text-sm">OS low-latency tuning</CardTitle>
          </CardHeader>
          <CardContent className="px-4">
            {Object.keys(tuning).length === 0 ? (
              <p className="text-[11px] text-muted-foreground">
                No tuning snapshot exposed by the gateway.
              </p>
            ) : (
              <KeyValues data={tuning} />
            )}
          </CardContent>
        </Card>
      </div>

      <Card className="border-border/70 bg-card/60 shadow-none">
        <CardHeader className="px-4">
          <CardTitle className="text-sm">Prometheus / OpenTelemetry metrics</CardTitle>
          <p className="text-[11px] text-muted-foreground">
            Scrape targets: <span className="font-mono">/metrics</span> and{' '}
            <span className="font-mono">/api/v2/metrics</span>
          </p>
        </CardHeader>
        <CardContent className="px-4">
          {metrics.error ? (
            <p className="text-[11px] text-amber-400">{metrics.error}</p>
          ) : metrics.loading ? (
            <p className="text-[11px] text-muted-foreground">Loading telemetry…</p>
          ) : (
            <KeyValues data={asRecord(metrics.data)} />
          )}
        </CardContent>
      </Card>
    </PageShell>
  )
}

function proofBadge(value: boolean): string {
  return value ? 'HOLDS' : 'VIOLATED'
}

function asRecord(value: unknown): Record<string, unknown> {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }
  return {}
}

function asRows(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return []
  return value.map((row) => asRecord(row))
}

function numberish(value: unknown): number {
  if (typeof value === 'number' && Number.isFinite(value)) return Math.round(value)
  if (typeof value === 'string') {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return Math.round(parsed)
  }
  return 0
}

function KeyValues({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data).filter(
    ([, value]) =>
      value === null ||
      typeof value === 'string' ||
      typeof value === 'number' ||
      typeof value === 'boolean'
  )
  if (entries.length === 0) {
    return <p className="text-[11px] text-muted-foreground">No scalar fields reported.</p>
  }
  return (
    <div className="grid gap-1 font-mono text-[11px]">
      {entries.map(([key, value]) => (
        <div key={key} className="flex items-center justify-between gap-2">
          <span className="text-muted-foreground">{key}</span>
          <span className="tabular-nums">{cell({ value }, 'value')}</span>
        </div>
      ))}
    </div>
  )
}
