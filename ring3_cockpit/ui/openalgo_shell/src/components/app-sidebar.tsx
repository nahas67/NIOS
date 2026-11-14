import { useState } from 'react'
import {
  Activity,
  BookOpen,
  Bot,
  BrainCircuit,
  Calculator,
  ChevronDown,
  ChevronRight,
  ClipboardList,
  Flame,
  FlaskConical,
  Layers,
  LayoutDashboard,
  LineChart,
  ListOrdered,
  ServerCog,
  ShieldCheck,
  SlidersHorizontal,
} from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { PAGE_PATHS, type PageKey } from '@/router'
import { cn } from '@/lib/utils'
import type { CockpitState } from '@/types'

export type SidebarGroupKey =
  | 'core'
  | 'quant'
  | 'brain'
  | 'ledger'
  | 'control'

interface NavItem {
  key: PageKey
  label: string
  hint: string
  icon: typeof LayoutDashboard
}

interface NavGroup {
  key: SidebarGroupKey
  label: string
  items: NavItem[]
}

/**
 * V1.3 sidebar taxonomy. The five groups are the operator's mental model of the
 * system, not the ring numbers: core execution first, then the quant desk, then
 * the bicameral brain, then the books, then the sovereign controls.
 */
export const NAV_GROUPS: NavGroup[] = [
  {
    key: 'core',
    label: 'Core Execution',
    items: [
      { key: 'dashboard', label: 'Dashboard', hint: 'Vaults · SSE', icon: LayoutDashboard },
      { key: 'terminal', label: 'Market Terminal', hint: 'L2 · candles', icon: Activity },
      { key: 'orders', label: 'Orders & Positions', hint: 'OMS · outbox', icon: ListOrdered },
    ],
  },
  {
    key: 'quant',
    label: 'Quant & Options',
    items: [
      { key: 'options', label: 'Options & Strategy', hint: 'chain · payoff', icon: Layers },
      { key: 'analyzer', label: 'Shadow Analyzer', hint: '30-fill nursery', icon: FlaskConical },
      { key: 'quant', label: 'Quant & Valuation', hint: '490 factors', icon: Calculator },
    ],
  },
  {
    key: 'brain',
    label: 'Bicameral Brain & Agents',
    items: [
      { key: 'agents', label: 'Agent Tasks', hint: '6 agents · 8 strategies', icon: Bot },
      { key: 'backtest', label: 'Backtester', hint: 'walk-forward · DSR', icon: LineChart },
      { key: 'brain', label: 'Brain & X-Ray', hint: 'rings · contagion', icon: BrainCircuit },
      { key: 'forge', label: 'Alpha Forge', hint: '00:00 UTC', icon: Flame },
    ],
  },
  {
    key: 'ledger',
    label: 'Ledger & Verification',
    items: [
      { key: 'ledger', label: 'IBOR Ledger', hint: 'double entry', icon: BookOpen },
      { key: 'logbook', label: 'Trade Logbook', hint: 'P&L · R-multiple', icon: ClipboardList },
      { key: 'verification', label: 'Proofs & Chaos', hint: 'TLA+ · VOPR', icon: ShieldCheck },
    ],
  },
  {
    key: 'control',
    label: 'Sovereign Control',
    items: [
      { key: 'brokers', label: 'Broker Hub', hint: '12 venues · feeds', icon: ServerCog },
      { key: 'control', label: 'Control & MCP', hint: '22 gates', icon: SlidersHorizontal },
    ],
  },
]

/** Flat page list, kept for callers that predate the grouped sidebar. */
export const NAV_ITEMS: NavItem[] = NAV_GROUPS.flatMap((group) => group.items)

type ShellMode = 'paper' | 'live' | 'legacy'

interface AppSidebarProps {
  page: PageKey
  onNavigate: (path: string) => void
  state: CockpitState | null
  connected: boolean
  /** Which shell is rendering the rail; tint and rail armour follow the mode. */
  mode?: ShellMode
}

/**
 * Collapsible navigation rail with five grouped sections.
 *
 * The rail is the same component in all three shells, but it never looks the
 * same: Paper is lavender/cyan, Live is crimson/amber with an armoured edge,
 * and the legacy gateway keeps the neutral slate palette.
 */
export function AppSidebar({
  page,
  onNavigate,
  state,
  connected,
  mode = 'legacy',
}: AppSidebarProps) {
  const status = state?.system.status ?? (connected ? 'LIVE' : 'OFFLINE')
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})

  const accentText =
    mode === 'live'
      ? 'text-rose-200'
      : mode === 'paper'
        ? 'text-cyan-200'
        : 'text-sky-300'
  const activeRow =
    mode === 'live'
      ? 'bg-rose-500/15 text-rose-50'
      : mode === 'paper'
        ? 'bg-cyan-500/12 text-cyan-50'
        : 'bg-primary/10 text-foreground'

  function toggle(key: SidebarGroupKey) {
    setCollapsed((current) => ({ ...current, [key]: !current[key] }))
  }

  return (
    <>
      <aside
        className={cn(
          'sticky top-0 hidden h-screen w-64 shrink-0 flex-col border-r border-border/60 bg-card/40 backdrop-blur-xl lg:flex',
          mode === 'live' && 'nios-armored-rail',
        )}
      >
        <div className="border-b border-border/60 px-4 py-3">
          <div className="flex items-center gap-2">
            <span
              className={cn(
                'size-2 rounded-full',
                mode === 'live'
                  ? 'bg-rose-400 shadow-[0_0_10px_rgba(244,63,94,0.9)]'
                  : 'bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.9)]',
              )}
            />
            <span className="font-mono text-xs font-semibold tracking-wider">NIOS · RING 3</span>
          </div>
          <p className="mt-1 text-[10px] text-muted-foreground">
            4-ring air-gapped trading cockpit · 15 pages
          </p>
        </div>

        <nav aria-label="Cockpit pages" className="flex-1 overflow-y-auto px-2 py-3">
          {NAV_GROUPS.map((group) => {
            const isCollapsed = Boolean(collapsed[group.key])
            return (
              <section key={group.key} className="mb-2">
                <button
                  type="button"
                  onClick={() => toggle(group.key)}
                  aria-expanded={!isCollapsed}
                  className="flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-accent/30"
                >
                  {isCollapsed ? (
                    <ChevronRight className="size-3 text-muted-foreground" />
                  ) : (
                    <ChevronDown className="size-3 text-muted-foreground" />
                  )}
                  <span className="nios-group-label">{group.label}</span>
                  <span className="ml-auto font-mono text-[9px] text-muted-foreground/70">
                    {group.items.length}
                  </span>
                </button>

                {isCollapsed
                  ? null
                  : group.items.map((item) => {
                      const Icon = item.icon
                      const active = item.key === page
                      return (
                        <button
                          key={item.key}
                          type="button"
                          aria-current={active ? 'page' : undefined}
                          onClick={() => onNavigate(PAGE_PATHS[item.key])}
                          className={cn(
                            'flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left transition-colors',
                            active
                              ? activeRow
                              : 'text-muted-foreground hover:bg-accent/40 hover:text-foreground',
                          )}
                        >
                          <Icon className={cn('size-4 shrink-0', active && accentText)} />
                          <span className="min-w-0 flex-1">
                            <span className="block truncate text-xs font-medium">{item.label}</span>
                            <span className="block truncate text-[9px] text-muted-foreground">
                              {item.hint}
                            </span>
                          </span>
                        </button>
                      )
                    })}
              </section>
            )
          })}
        </nav>

        <div className="border-t border-border/60 px-4 py-3">
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="font-mono text-[9px]">
              {status}
            </Badge>
            <Badge
              variant="outline"
              className={cn(
                'font-mono text-[9px]',
                mode === 'live'
                  ? 'border-rose-500/50 text-rose-200'
                  : mode === 'paper'
                    ? 'border-cyan-500/50 text-cyan-200'
                    : 'border-border/60',
              )}
            >
              {mode === 'live' ? 'LIVE' : mode === 'paper' ? 'PAPER' : 'GATEWAY'}
            </Badge>
          </div>
          <p className="mt-2 text-[9px] leading-relaxed text-muted-foreground">
            Ring 3 is read-only for market state. Zero order-signing tools, zero key-reading
            tools.
          </p>
        </div>
      </aside>

      <div className="sticky top-0 z-20 flex gap-1 overflow-x-auto border-b border-border/60 bg-background/95 px-2 py-2 backdrop-blur lg:hidden">
        {NAV_ITEMS.map((item) => {
          const active = item.key === page
          return (
            <button
              key={item.key}
              type="button"
              aria-current={active ? 'page' : undefined}
              onClick={() => onNavigate(PAGE_PATHS[item.key])}
              className={cn(
                'shrink-0 rounded-md border px-2.5 py-1 text-[10px] font-medium transition-colors',
                active
                  ? 'border-sky-400/50 bg-primary/10 text-foreground'
                  : 'border-border/60 text-muted-foreground',
              )}
            >
              {item.label}
            </button>
          )
        })}
      </div>
    </>
  )
}
