import { FileCode2, Flame, Plug, Rocket, ShieldCheck, Timer } from 'lucide-react'
import { useState } from 'react'

import { ActionFeedback, ActionPanel, ProofRow } from '@/components/operator-action'
import { PageShell, StatTile, DataState } from '@/components/page-shell'
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
import { useAction } from '@/hooks/use-action'
import { usePageData } from '@/hooks/use-page-data'
import { fetchForgePage, fetchSkillMarkdown, postEvaluateChallenger } from '@/lib/api'
import { formatBps } from '@/lib/format'
import { cell, num } from '@/lib/rows'
import type { ChallengerEvalReceipt, SkillMarkdownReceipt } from '@/types'

const SILO_IDS = ['CRYPTO_BINANCE', 'INDIA_ZERODHA_DHAN', 'US_IBKR', 'FX_MT5'] as const

export function ForgePage() {
  const { data, error, loading, reload } = usePageData(fetchForgePage)
  const leaderboard = data?.leaderboard ?? []
  const thresholds = data?.thresholds ?? {}
  const plugins = data?.plugins ?? {}
  const skills = data?.skills ?? {}
  const promoteDsr = Number(thresholds.promote_deflated_sharpe ?? thresholds.promote_dsr ?? 1.5)
  const demoteBps = Number(thresholds.demote_net_edge_bps ?? 0.5)
  const seconds = data?.next_run?.seconds_until_next_run ?? 0

  const [selectedSkill, setSelectedSkill] = useState<string | null>(null)
  const detail = useAction<SkillMarkdownReceipt>()

  const [pluginName, setPluginName] = useState('genome_alpha_7')
  const [sharpe, setSharpe] = useState('1.85')
  const [fills, setFills] = useState('30')
  const [silo, setSilo] = useState<string>('CRYPTO_BINANCE')
  const evaluation = useAction<ChallengerEvalReceipt>()

  async function inspectSkill(name: string) {
    setSelectedSkill(name)
    await detail.run(() => fetchSkillMarkdown(name))
  }

  async function evaluateChallenger() {
    const receipt = await evaluation.run(() =>
      postEvaluateChallenger({
        plugin_name: pluginName.trim(),
        candidate_sharpe: Number.parseFloat(sharpe) || 0,
        shadow_fills: Number.parseInt(fills, 10) || 0,
        silo,
      })
    )
    if (receipt) reload()
  }

  return (
    <PageShell
      title="Nocturnal Alpha Forge & Plugins"
      ring="RING 2"
      capability="PAPER CONTROL"
      subtitle="00:00 UTC walk-forward forge. A challenger only replaces a champion with Deflated Sharpe ≥ 1.50 and a positive rolling net edge."
      actions={
        <Badge variant="outline" className="font-mono text-[10px]">
          NEXT RUN {formatCountdown(seconds)}
        </Badge>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          icon={Flame}
          label="Champions"
          value={`${num(data?.forge ?? {}, 'champion_count')}`}
          hint={`Demote below ${formatBps(demoteBps)} net edge`}
        />
        <StatTile
          icon={Rocket}
          label="Challengers"
          value={`${num(data?.forge ?? {}, 'challenger_count')}`}
          hint={`Promote at Deflated Sharpe ≥ ${promoteDsr.toFixed(2)}`}
        />
        <StatTile
          icon={Plug}
          label="Plugins"
          value={`${num(plugins, 'count')}`}
          hint="Register, toggle and shadow-grade without a restart"
        />
        <StatTile
          icon={Timer}
          label="Skills"
          value={`${num(skills, 'count')}`}
          hint="Progressive-disclosure SKILL.md catalog"
        />
      </div>

      <DataState loading={loading} error={error}>
        <ActionPanel
          title="Evaluate Challenger Genome / Plugin"
          description="Registers the candidate in the Shadow nursery and enforces both gates from the forge itself: Deflated Sharpe ≥ 1.50 and ≥ 30 shadow fills. Admission only — no plugin code is executed, deployed or signed."
        >
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <Label
                htmlFor="challenger-name"
                className="text-[10px] uppercase tracking-wider text-muted-foreground"
              >
                Genome / plugin
              </Label>
              <Input
                id="challenger-name"
                value={pluginName}
                onChange={(event) => setPluginName(event.target.value)}
                className="h-8 w-52 font-mono text-xs"
              />
            </div>
            <div className="space-y-1">
              <Label
                htmlFor="challenger-sharpe"
                className="text-[10px] uppercase tracking-wider text-muted-foreground"
              >
                Deflated Sharpe
              </Label>
              <Input
                id="challenger-sharpe"
                type="number"
                step="0.01"
                value={sharpe}
                onChange={(event) => setSharpe(event.target.value)}
                className="h-8 w-28 font-mono text-xs"
              />
            </div>
            <div className="space-y-1">
              <Label
                htmlFor="challenger-fills"
                className="text-[10px] uppercase tracking-wider text-muted-foreground"
              >
                Shadow fills
              </Label>
              <Input
                id="challenger-fills"
                type="number"
                min={0}
                value={fills}
                onChange={(event) => setFills(event.target.value)}
                className="h-8 w-24 font-mono text-xs"
              />
            </div>
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
            <Button
              type="button"
              size="sm"
              disabled={evaluation.pending || pluginName.trim().length === 0}
              onClick={evaluateChallenger}
            >
              <ShieldCheck className="mr-1 size-3.5" />
              Evaluate Challenger
            </Button>
          </div>

          <div className="mt-3">
            <ActionFeedback pending={evaluation.pending} error={evaluation.error}>
              {evaluation.receipt ? (
                <div className="rounded-lg border border-border/70 bg-background/40 p-3">
                  <div className="mb-2 flex flex-wrap items-center gap-2">
                    <Badge
                      variant={evaluation.receipt.promotion_ready ? 'default' : 'destructive'}
                      className="font-mono text-[10px]"
                    >
                      {evaluation.receipt.status}
                    </Badge>
                    <span className="font-mono text-[10px] text-muted-foreground">
                      {evaluation.receipt.plugin_name} · {evaluation.receipt.genome_id} ·{' '}
                      {evaluation.receipt.silo}
                    </span>
                    <Badge variant="outline" className="font-mono text-[9px]">
                      {evaluation.receipt.activation}
                    </Badge>
                  </div>
                  <div className="grid gap-x-6 sm:grid-cols-2">
                    <ProofRow
                      label="Deflated Sharpe ≥ 1.50"
                      value={gateLabel(
                        evaluation.receipt.gate_results.dsr_floor_met,
                        evaluation.receipt.candidate_sharpe.toFixed(4)
                      )}
                    />
                    <ProofRow
                      label="Shadow fills ≥ 30"
                      value={gateLabel(
                        evaluation.receipt.gate_results.shadow_nursery_met,
                        String(evaluation.receipt.shadow_fills)
                      )}
                    />
                    <ProofRow
                      label="Nursery graduated"
                      value={gateLabel(
                        evaluation.receipt.gate_results.nursery_graduated,
                        String(evaluation.receipt.nursery.graduated ?? false)
                      )}
                    />
                    <ProofRow
                      label="Recorded fills"
                      value={String(evaluation.receipt.recorded_fills)}
                    />
                    <ProofRow
                      label="Champions"
                      value={String(evaluation.receipt.forge.champion_count ?? 0)}
                    />
                    <ProofRow
                      label="Challengers"
                      value={String(evaluation.receipt.forge.challenger_count ?? 0)}
                    />
                  </div>
                  {evaluation.receipt.rejection_reasons.length > 0 ? (
                    <ul className="mt-2 space-y-1">
                      {evaluation.receipt.rejection_reasons.map((reason) => (
                        <li
                          key={reason}
                          role="alert"
                          className="rounded-md border border-amber-500/40 bg-amber-500/5 px-2 py-1 font-mono text-[10px] text-amber-300"
                        >
                          {reason}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="mt-2 text-[11px] text-muted-foreground">
                      Both nursery gates hold. The genome waits in shadow until the 00:00 UTC forge
                      run decides deployment.
                    </p>
                  )}
                </div>
              ) : !evaluation.pending ? (
                <p className="text-[11px] text-muted-foreground">
                  No evaluation yet. A candidate never reaches the roster on a promise — both gates
                  are read from the forge.
                </p>
              ) : null}
            </ActionFeedback>
          </div>
        </ActionPanel>

        <Card className="border-border/70 bg-card/60 shadow-none">
          <CardHeader className="px-4">
            <CardTitle className="text-sm">Champion / challenger leaderboard</CardTitle>
            <p className="text-[11px] text-muted-foreground">
              Rolling net edge is measured after spread, fees and statutory tax floors.
            </p>
          </CardHeader>
          <CardContent className="px-4">
            {leaderboard.length === 0 ? (
              <p className="rounded-lg border border-dashed border-border/70 px-3 py-6 text-center text-xs text-muted-foreground">
                No genomes evaluated yet — the forge runs at 00:00 UTC.
              </p>
            ) : (
              <table className="w-full text-left text-[11px]">
                <thead className="text-[10px] uppercase tracking-wider text-muted-foreground">
                  <tr>
                    <th className="py-1 pr-3 font-medium">Strategy</th>
                    <th className="py-1 pr-3 font-medium">Role</th>
                    <th className="py-1 pr-3 font-medium">Deflated Sharpe</th>
                    <th className="py-1 pr-3 font-medium">Net edge</th>
                    <th className="py-1 font-medium">Gate</th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {leaderboard.map((row) => (
                    <tr
                      key={`${row.role}-${row.strategy_id}`}
                      className="border-t border-border/40"
                    >
                      <td className="py-1 pr-3 font-medium">{row.strategy_id}</td>
                      <td className="py-1 pr-3 text-muted-foreground">{row.role}</td>
                      <td className="py-1 pr-3 tabular-nums">
                        {(row.deflated_sharpe ?? 0).toFixed(2)}
                      </td>
                      <td className="py-1 pr-3 tabular-nums">{formatBps(row.net_edge_bps ?? 0)}</td>
                      <td className="py-1">
                        <Badge
                          variant={row.promotion_ready && row.above_floor ? 'default' : 'outline'}
                          className="font-mono text-[9px]"
                        >
                          {row.promotion_ready && row.above_floor ? 'PROMOTE' : 'HOLD'}
                        </Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </CardContent>
        </Card>

        <div className="grid gap-3 lg:grid-cols-2">
          <Card className="border-border/70 bg-card/60 shadow-none">
            <CardHeader className="px-4">
              <CardTitle className="text-sm">Promotion thresholds</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-1.5 px-4 font-mono text-[11px]">
              {Object.entries(thresholds).map(([key, value]) => (
                <div key={key} className="flex items-center justify-between gap-2">
                  <span className="text-muted-foreground">{key}</span>
                  <span className="tabular-nums">{cell({ value }, 'value')}</span>
                </div>
              ))}
            </CardContent>
          </Card>

          <Card className="border-border/70 bg-card/60 shadow-none">
            <CardHeader className="px-4">
              <CardTitle className="text-sm">Registered challenger plugins & skills</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-wrap items-center gap-1.5 px-4">
              {pluginNames(plugins).map((name) => (
                <Badge key={`plugin-${name}`} variant="outline" className="font-mono text-[10px]">
                  {name}
                </Badge>
              ))}
              {skillNames(skills).map((name) => (
                <button
                  key={`skill-${name}`}
                  type="button"
                  onClick={() => inspectSkill(name)}
                  title="Inspect Full SKILL.md"
                  className="rounded-full transition-opacity hover:opacity-75 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <Badge
                    variant={selectedSkill === name ? 'default' : 'secondary'}
                    className="cursor-pointer font-mono text-[10px]"
                  >
                    {name}
                  </Badge>
                </button>
              ))}
              {pluginNames(plugins).length === 0 && skillNames(skills).length === 0 ? (
                <p className="text-[11px] text-muted-foreground">No plugins or skills registered.</p>
              ) : null}
              {skillNames(skills).length > 0 ? (
                <p className="w-full text-[10px] text-muted-foreground">
                  Select a skill badge to Inspect Full SKILL.md (progressive disclosure, sec_016).
                </p>
              ) : null}
            </CardContent>
          </Card>
        </div>

        <Card className="border-border/70 bg-card/60 shadow-none">
          <CardHeader className="px-4">
            <CardTitle className="flex items-center gap-2 text-sm">
              <FileCode2 className="size-4" />
              Skill inspector — full SKILL.md on demand
            </CardTitle>
            <p className="text-[11px] text-muted-foreground">
              The gateway reads only inside the skill registry root; unknown and path-traversing
              names answer 404.
            </p>
          </CardHeader>
          <CardContent className="space-y-3 px-4">
            <ActionFeedback pending={detail.pending} error={detail.error}>
              {detail.receipt ? (
                <div className="space-y-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="outline" className="font-mono text-[10px]">
                      {detail.receipt.status}
                    </Badge>
                    <span className="font-mono text-[11px]">{detail.receipt.name}</span>
                    <Badge
                      variant={detail.receipt.enabled ? 'default' : 'secondary'}
                      className="font-mono text-[9px]"
                    >
                      {detail.receipt.enabled ? 'ENABLED' : 'DISABLED'}
                    </Badge>
                  </div>
                  <div className="grid gap-x-6 sm:grid-cols-2">
                    <ProofRow label="Path" value={detail.receipt.path} />
                    <ProofRow label="Bytes" value={String(detail.receipt.bytes)} />
                    <ProofRow
                      label="Progressive disclosure"
                      value={detail.receipt.progressive_disclosure ? 'YES' : 'NO'}
                    />
                    <ProofRow
                      label="Frontmatter keys"
                      value={Object.keys(detail.receipt.frontmatter).join(', ') || 'none'}
                    />
                  </div>
                  {detail.receipt.description ? (
                    <p className="text-[11px] text-muted-foreground">{detail.receipt.description}</p>
                  ) : null}
                  <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg border border-border/70 bg-background/60 p-3 font-mono text-[11px] leading-relaxed">
                    {detail.receipt.markdown}
                  </pre>
                </div>
              ) : !detail.pending ? (
                <p className="rounded-lg border border-dashed border-border/70 px-3 py-6 text-center text-xs text-muted-foreground">
                  No skill inspected yet. Choose a skill badge above to load its full Markdown body.
                </p>
              ) : null}
            </ActionFeedback>
          </CardContent>
        </Card>
      </DataState>
    </PageShell>
  )
}

function formatCountdown(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '00:00 UTC'
  const total = Math.round(seconds)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  return `${hours}h ${String(minutes).padStart(2, '0')}m`
}

function gateLabel(passed: boolean, measured: string): string {
  return `${passed ? 'PASS' : 'FAIL'} · ${measured}`
}

function pluginNames(plugins: Record<string, unknown>): string[] {
  const rows = plugins.plugins
  if (!Array.isArray(rows)) return []
  return rows
    .map((row) => (row && typeof row === 'object' ? (row as Record<string, unknown>) : null))
    .map((row) => (row ? String(row.name ?? row.key ?? '') : ''))
    .filter((name) => name.length > 0)
}

function skillNames(skills: Record<string, unknown>): string[] {
  const rows = skills.skills
  if (!Array.isArray(rows)) return []
  return rows
    .map((row) => (row && typeof row === 'object' ? (row as Record<string, unknown>) : null))
    .map((row) => (row ? String(row.name ?? row.key ?? '') : ''))
    .filter((name) => name.length > 0)
}
