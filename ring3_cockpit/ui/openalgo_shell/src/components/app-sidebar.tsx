import {
  BookOpen,
  Bot,
  BrainCircuit,
  Calculator,
  Flame,
  FlaskConical,
  LayoutDashboard,
  LineChart,
  ListOrdered,
  ShieldCheck,
  SlidersHorizontal,
} from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { PAGE_PATHS, type PageKey } from '@/router'
import { cn } from '@/lib/utils'
import type { CockpitState } from '@/types'

interface NavItem {
  key: PageKey
  label: string
  hint: string
  icon: typeof LayoutDashboard
}

const NAV_ITEMS: NavItem[] = [
  { key: 'dashboard', label: 'Dashboard', hint: 'Vaults · SSE', icon: LayoutDashboard },
  { key: 'orders', label: 'Orders & Positions', hint: 'OMS · outbox', icon: ListOrdered },
  { key: 'analyzer', label: 'Shadow Analyzer', hint: '30-fill nursery', icon: FlaskConical },
  { key: 'quant', label: 'Quant & Valuation', hint: '490 factors', icon: Calculator },
  { key: 'brain', label: 'Brain & X-Ray', hint: 'rings · contagion', icon: BrainCircuit },
  { key: 'forge', label: 'Alpha Forge', hint: '00:00 UTC', icon: Flame },
  { key: 'ledger', label: 'IBOR Ledger', hint: 'double entry', icon: BookOpen },
  { key: 'verification', label: 'Proofs & Chaos', hint: 'TLA+ · VOPR', icon: ShieldCheck },
  { key: 'control', label: 'Control & MCP', hint: '22 gates', icon: SlidersHorizontal },
  { key: 'agents', label: 'Agent Tasks', hint: '6 agents · 8 strategies', icon: Bot },
  { key: 'backtest', label: 'Backtester', hint: 'walk-forward · DSR', icon: LineChart },
]

interface AppSidebarProps {
  page: PageKey
  onNavigate: (path: string) => void
  state: CockpitState | null
  connected: boolean
}

/** Fixed left navigation rail plus a compact horizontal strip on small screens. */
export function AppSidebar({ page, onNavigate, state, connected }: AppSidebarProps) {
  const status = state?.system.status ?? (connected ? 'LIVE' : 'OFFLINE')

  return (
    <>
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col border-r border-border/60 bg-card/40 backdrop-blur lg:flex">
        <div className="border-b border-border/60 px-4 py-3">
          <div className="flex items-center gap-2">
            <span className="size-2 rounded-full bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.9)]" />
            <span className="font-mono text-xs font-semibold tracking-wider">NIOS · RING 3</span>
          </div>
          <p className="mt-1 text-[10px] text-muted-foreground">
            4-ring air-gapped trading cockpit
          </p>
        </div>

        <nav aria-label="Cockpit pages" className="flex-1 space-y-0.5 overflow-y-auto px-2 py-3">
          {NAV_ITEMS.map((item) => {
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
                    ? 'bg-primary/10 text-foreground'
                    : 'text-muted-foreground hover:bg-accent/40 hover:text-foreground'
                )}
              >
                <Icon className={cn('size-4 shrink-0', active && 'text-sky-300')} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-xs font-medium">{item.label}</span>
                  <span className="block truncate text-[9px] text-muted-foreground">
                    {item.hint}
                  </span>
                </span>
              </button>
            )
          })}
        </nav>

        <div className="border-t border-border/60 px-4 py-3">
          <Badge variant="outline" className="font-mono text-[9px]">
            {status}
          </Badge>
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
                  : 'border-border/60 text-muted-foreground'
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

export { NAV_ITEMS }
