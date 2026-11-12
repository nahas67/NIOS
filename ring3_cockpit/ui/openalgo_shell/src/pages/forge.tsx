import { Flame, Plug, Rocket, Timer } from 'lucide-react'

import { PageShell, StatTile, DataState } from '@/components/page-shell'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { usePageData } from '@/hooks/use-page-data'
import { fetchForgePage } from '@/lib/api'
import { formatBps } from '@/lib/format'
import { cell, num } from '@/lib/rows'

export function ForgePage() {
  const { data, error, loading } = usePageData(fetchForgePage)
  const leaderboard = data?.leaderboard ?? []
  const thresholds = data?.thresholds ?? {}
  const plugins = data?.plugins ?? {}
  const skills = data?.skills ?? {}
  const promoteDsr = Number(thresholds.promote_deflated_sharpe ?? thresholds.promote_dsr ?? 1.5)
  const demoteBps = Number(thresholds.demote_net_edge_bps ?? 0.5)
  const seconds = data?.next_run?.seconds_until_next_run ?? 0

  return (
    <PageShell
      title="Nocturnal Alpha Forge & Plugins"
      ring="RING 2"
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
            <CardContent className="flex flex-wrap gap-1.5 px-4">
              {pluginNames(plugins).map((name) => (
                <Badge key={`plugin-${name}`} variant="outline" className="font-mono text-[10px]">
                  {name}
                </Badge>
              ))}
              {skillNames(skills).map((name) => (
                <Badge key={`skill-${name}`} variant="secondary" className="font-mono text-[10px]">
                  {name}
                </Badge>
              ))}
              {pluginNames(plugins).length === 0 && skillNames(skills).length === 0 ? (
                <p className="text-[11px] text-muted-foreground">No plugins or skills registered.</p>
              ) : null}
            </CardContent>
          </Card>
        </div>
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
