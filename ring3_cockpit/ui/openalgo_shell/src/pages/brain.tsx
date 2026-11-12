import { BrainCircuit, Radio, Scale, ShieldCheck, Waves } from 'lucide-react'
import { useState } from 'react'

import { BrainXrayTab } from '@/components/brain-xray-tab'
import { ActionFeedback, ActionPanel, ProofRow } from '@/components/operator-action'
import { PageShell, StatTile } from '@/components/page-shell'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useAction } from '@/hooks/use-action'
import { postCommitteeDebate, postMiroFishShock } from '@/lib/api'
import type {
  CockpitState,
  CommitteeDebateReceipt,
  MiroFishShockReceipt,
} from '@/types'

const SILO_IDS = ['CRYPTO_BINANCE', 'INDIA_ZERODHA_DHAN', 'US_IBKR', 'FX_MT5'] as const

interface BrainPageProps {
  state: CockpitState | null
}

export function BrainPage({ state }: BrainPageProps) {
  const processes = state?.ring_processes
  const contagion = state?.contagion

  const [symbol, setSymbol] = useState('BTCUSDT')
  const [shockBps, setShockBps] = useState('-180')
  const [sentimentTag, setSentimentTag] = useState('panic')
  const shock = useAction<MiroFishShockReceipt>()

  const [silo, setSilo] = useState<string>('CRYPTO_BINANCE')
  const [debateSymbol, setDebateSymbol] = useState('AUTO')
  const debate = useAction<CommitteeDebateReceipt>()

  async function runShock() {
    await shock.run(() =>
      postMiroFishShock({
        symbol: symbol.trim().toUpperCase() || 'BTCUSDT',
        shock_bps: Number.parseFloat(shockBps) || 0,
        sentiment_tag: sentimentTag.trim() || 'panic',
      })
    )
  }

  async function runDebate() {
    await debate.run(() =>
      postCommitteeDebate({
        silo,
        symbol: debateSymbol.trim().toUpperCase() || 'AUTO',
      })
    )
  }

  return (
    <PageShell
      title="Brain & Contagion X-Ray"
      ring="RING 2"
      capability="PAPER CONTROL"
      subtitle="Four-ring OS process isolation, MiroFish three-tick contagion cascade, the staged investment committee and the SHA-256 grounding ledger."
      actions={
        <Badge variant="outline" className="font-mono text-[10px]">
          {processes?.distinct_os_processes ?? 0} OS PROCESSES
        </Badge>
      }
    >
      <div className="grid gap-3 sm:grid-cols-3">
        <StatTile
          icon={Radio}
          label="Feed mode"
          value={String(state?.feed_mode ?? 'DUAL_VENDOR')}
          hint={`${state?.brokers?.order?.length ?? 0} broker silos wired`}
        />
        <StatTile
          icon={BrainCircuit}
          label="Cascade fragility"
          value={(contagion?.cascade_fragility_index ?? 0).toFixed(2)}
          hint={
            (contagion?.cascade_fragility_index ?? 0) >= 0.75
              ? 'Above 0.75 — long leases revoked'
              : 'Below the 0.75 revocation threshold'
          }
        />
        <StatTile
          icon={ShieldCheck}
          label="Grounded facts"
          value={`${state?.contagion?.grounding_ledger?.entries ?? 0}`}
          hint="Every numeric claim carries a SHA-256 commitment"
        />
      </div>

      <div className="grid gap-3 xl:grid-cols-2">
        <ActionPanel
          title="MiroFish Shock Simulator"
          description="Five archetypes — Retail 0DTE, Options Dealer Gamma, Vol-Target CTA, Pod-Shop Stops, Value Funds — across three ticks. The SLA is < 1.8 ms and the digest commits to the inputs and outputs."
        >
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <Label
                htmlFor="shock-symbol"
                className="text-[10px] uppercase tracking-wider text-muted-foreground"
              >
                Symbol
              </Label>
              <Input
                id="shock-symbol"
                value={symbol}
                onChange={(event) => setSymbol(event.target.value)}
                className="h-8 w-36 font-mono text-xs"
              />
            </div>
            <div className="space-y-1">
              <Label
                htmlFor="shock-bps"
                className="text-[10px] uppercase tracking-wider text-muted-foreground"
              >
                Shock bps
              </Label>
              <Input
                id="shock-bps"
                type="number"
                value={shockBps}
                onChange={(event) => setShockBps(event.target.value)}
                className="h-8 w-28 font-mono text-xs"
              />
            </div>
            <div className="space-y-1">
              <Label
                htmlFor="shock-tag"
                className="text-[10px] uppercase tracking-wider text-muted-foreground"
              >
                Sentiment tag
              </Label>
              <Select value={sentimentTag} onValueChange={setSentimentTag}>
                <SelectTrigger id="shock-tag" className="h-8 w-44 font-mono text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SENTIMENT_TAGS.map((tag) => (
                    <SelectItem key={tag} value={tag} className="font-mono text-xs">
                      {tag}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Button type="button" size="sm" disabled={shock.pending} onClick={runShock}>
              <Waves className="mr-1 size-3.5" />
              Run 3-Tick Cascade
            </Button>
          </div>

          <div className="mt-3">
            <ActionFeedback pending={shock.pending} error={shock.error}>
              {shock.receipt ? (
                <div className="space-y-3 rounded-lg border border-border/70 bg-background/40 p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge
                      variant={
                        shock.receipt.cascade_fragility_index >=
                        shock.receipt.fragility_halt_threshold
                          ? 'destructive'
                          : 'outline'
                      }
                      className="font-mono text-[10px]"
                    >
                      FRAGILITY {shock.receipt.cascade_fragility_index.toFixed(3)}
                    </Badge>
                    <Badge
                      variant={shock.receipt.within_sla ? 'default' : 'destructive'}
                      className="font-mono text-[10px]"
                    >
                      {shock.receipt.latency_ms.toFixed(3)} ms {shock.receipt.within_sla ? '< 1.8 ms' : 'SLA BREACH'}
                    </Badge>
                    <Badge
                      variant={shock.receipt.requires_ring1_halt ? 'destructive' : 'outline'}
                      className="font-mono text-[10px]"
                    >
                      {shock.receipt.requires_ring1_halt ? 'RING 1 HALT' : 'NO HALT'}
                    </Badge>
                    <span className="font-mono text-[10px] text-muted-foreground">
                      {shock.receipt.symbol} · {shock.receipt.archetype_count} archetypes ·{' '}
                      {shock.receipt.tick_count} ticks
                    </span>
                  </div>

                  <div className="space-y-2">
                    {shock.receipt.tick_breakdown.map((tick) => (
                      <div
                        key={`tick-${tick.tick}`}
                        className="rounded-md border border-border/40 bg-background/40 px-2 py-1.5"
                      >
                        <div className="flex items-center justify-between font-mono text-[10px]">
                          <span className="text-muted-foreground">tick {tick.tick}</span>
                          <span className="tabular-nums">net {tick.net_flow.toFixed(6)}</span>
                        </div>
                        <div className="mt-1 flex flex-wrap gap-1">
                          {tick.archetypes.map((row) => (
                            <Badge
                              key={`${tick.tick}-${row.name}`}
                              variant="secondary"
                              className="font-mono text-[9px]"
                            >
                              {row.name} {row.flow.toFixed(4)}
                            </Badge>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>

                  <div className="grid gap-x-6 sm:grid-cols-2">
                    <ProofRow
                      label="Crowd asymmetry"
                      value={shock.receipt.crowd_asymmetry_score.toFixed(6)}
                    />
                    <ProofRow
                      label="Narrative exhaustion"
                      value={shock.receipt.narrative_exhaustion_divergence.toFixed(6)}
                    />
                    <ProofRow
                      label="Revoked long leases"
                      value={String(shock.receipt.revoked_long_leases)}
                    />
                    <ProofRow
                      label="Engine latency"
                      value={`${shock.receipt.engine_latency_ms.toFixed(3)} ms`}
                    />
                  </div>
                  <p className="break-all font-mono text-[10px] text-muted-foreground">
                    SHA-256 {shock.receipt.sha256_digest}
                  </p>
                </div>
              ) : !shock.pending ? (
                <p className="text-[11px] text-muted-foreground">
                  No cascade yet. A shock is simulated, never inferred from a resting quote
                  (sec_018 Law 1).
                </p>
              ) : null}
            </ActionFeedback>
          </div>
        </ActionPanel>

        <ActionPanel
          title="Run System 2 Committee Debate"
          description="Bull, Bear, Risk Manager and Portfolio Manager debate a grounded candidate. Every figure is re-computed by the numerical verifier; a fabricated number aborts the debate."
        >
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">
                Silo
              </Label>
              <Select value={silo} onValueChange={setSilo}>
                <SelectTrigger className="h-8 w-52 font-mono text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SILO_IDS.map((value) => (
                    <SelectItem key={value} value={value} className="font-mono text-xs">
                      {value}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label
                htmlFor="debate-symbol"
                className="text-[10px] uppercase tracking-wider text-muted-foreground"
              >
                Symbol
              </Label>
              <Input
                id="debate-symbol"
                value={debateSymbol}
                onChange={(event) => setDebateSymbol(event.target.value)}
                placeholder="AUTO"
                className="h-8 w-32 font-mono text-xs"
              />
            </div>
            <Button type="button" size="sm" disabled={debate.pending} onClick={runDebate}>
              <Scale className="mr-1 size-3.5" />
              Run System 2 Committee Debate
            </Button>
          </div>

          <div className="mt-3">
            <ActionFeedback pending={debate.pending} error={debate.error}>
              {debate.receipt ? (
                <div className="space-y-3 rounded-lg border border-border/70 bg-background/40 p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge
                      variant={debate.receipt.verdict === 'CONVICTED' ? 'default' : 'secondary'}
                      className="font-mono text-[10px]"
                    >
                      {debate.receipt.verdict}
                    </Badge>
                    <span className="font-mono text-[10px] text-muted-foreground">
                      {debate.receipt.symbol} · rank {debate.receipt.rank} · {debate.receipt.stage_count}{' '}
                      stages · {debate.receipt.elapsed_ms.toFixed(1)} ms
                    </span>
                    {!debate.receipt.symbol_match ? (
                      <Badge variant="outline" className="font-mono text-[9px]">
                        requested {debate.receipt.requested_symbol}
                      </Badge>
                    ) : null}
                  </div>
                  {debate.receipt.abstention_reason ? (
                    <p className="text-[11px] text-amber-300">
                      {debate.receipt.abstention_reason}
                    </p>
                  ) : null}
                  <div className="max-h-80 space-y-1.5 overflow-auto pr-1">
                    {debate.receipt.transcript.map((stage) => (
                      <div
                        key={stage.stage}
                        className="rounded-md border border-border/40 bg-background/40 px-2 py-1.5"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-mono text-[10px] font-medium">{stage.actor}</span>
                          <span className="flex items-center gap-2">
                            <span className="font-mono text-[9px] text-muted-foreground">
                              {stage.claims_checked} claims
                            </span>
                            <Badge
                              variant={stage.verified ? 'outline' : 'destructive'}
                              className="font-mono text-[9px]"
                            >
                              {stage.verified ? 'VERIFIED' : 'UNGROUNDED'}
                            </Badge>
                          </span>
                        </div>
                        <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
                          {stage.text}
                        </p>
                      </div>
                    ))}
                  </div>
                  <p className="break-all font-mono text-[10px] text-muted-foreground">
                    SHA-256 packet {debate.receipt.sha256_digest}
                  </p>
                </div>
              ) : !debate.pending ? (
                <p className="text-[11px] text-muted-foreground">
                  No debate yet. The committee is a proposal layer: it can never sign an order
                  (sec_004).
                </p>
              ) : null}
            </ActionFeedback>
          </div>
        </ActionPanel>
      </div>

      <BrainXrayTab state={state} />
    </PageShell>
  )
}

const SENTIMENT_TAGS = [
  'panic',
  'retail_panic',
  'retail_euphoria',
  'dealer_gamma_flip',
  'cta_deleveraging',
  'pod_shop_stop_out',
  'value_absorption',
] as const
