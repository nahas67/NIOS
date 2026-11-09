import { AlertTriangle, BrainCircuit, LayoutDashboard, SlidersHorizontal, X } from 'lucide-react'

import { BrainXrayTab } from '@/components/brain-xray-tab'
import { ControlRoomTab } from '@/components/control-room-tab'
import { LiveCockpitTab } from '@/components/live-cockpit-tab'
import { TopBar } from '@/components/top-bar'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useCockpit } from '@/hooks/use-cockpit'

export default function App() {
  const cockpit = useCockpit()
  const { state, cards, connected, error, killReport, rearmReport, lastFrameAt, refresh, kill, rearm } =
    cockpit

  return (
    <div className="dark">
      <div className="relative min-h-screen overflow-hidden bg-background text-foreground">
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 opacity-60"
          style={{
            backgroundImage:
              'radial-gradient(circle at 12% 0%, rgba(56,189,248,0.10), transparent 42%), radial-gradient(circle at 88% 8%, rgba(168,85,247,0.10), transparent 40%), linear-gradient(rgba(148,163,184,0.05) 1px, transparent 1px), linear-gradient(90deg, rgba(148,163,184,0.05) 1px, transparent 1px)',
            backgroundSize: 'auto, auto, 48px 48px, 48px 48px',
          }}
        />

        <TopBar
          state={state}
          connected={connected}
          killReport={killReport}
          rearmReport={rearmReport}
          lastFrameAt={lastFrameAt}
          onKill={() => void kill()}
          onRearm={() => void rearm()}
        />

        {error ? (
          <div className="relative z-10 mx-4 mt-3 flex items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200 lg:mx-6">
            <AlertTriangle className="size-4 shrink-0" />
            <span className="flex-1">
              Ring 3 gateway: {error}. The cockpit will retry automatically.
            </span>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-6 px-2 text-amber-200 hover:bg-amber-500/10"
              onClick={cockpit.dismissError}
              aria-label="Dismiss gateway error"
            >
              <X className="size-3.5" />
            </Button>
          </div>
        ) : null}

        <main className="relative z-10 px-4 pb-10 pt-4 lg:px-6">
          <Tabs defaultValue="live" className="gap-4">
            <TabsList className="grid h-10 w-full max-w-[560px] grid-cols-3">
              <TabsTrigger value="live" className="gap-1.5 text-xs">
                <LayoutDashboard className="size-3.5" />
                Live Cockpit
              </TabsTrigger>
              <TabsTrigger value="xray" className="gap-1.5 text-xs">
                <BrainCircuit className="size-3.5" />
                Brain &amp; Contagion
              </TabsTrigger>
              <TabsTrigger value="control" className="gap-1.5 text-xs">
                <SlidersHorizontal className="size-3.5" />
                Control Room
              </TabsTrigger>
            </TabsList>

            <TabsContent value="live">
              <LiveCockpitTab state={state} cards={cards} />
            </TabsContent>
            <TabsContent value="xray">
              <BrainXrayTab state={state} />
            </TabsContent>
            <TabsContent value="control">
              <ControlRoomTab state={state} onSaved={refresh} />
            </TabsContent>
          </Tabs>

          <footer className="mt-8 flex flex-wrap items-center justify-between gap-2 border-t border-border/60 pt-3 text-[10px] text-muted-foreground">
            <span>
              NIOS-ARC-2026-V3 · v{state?.version ?? '3.0.0-PROD'} · Ring 3 is read-only
              for market state; writes are limited to the control endpoints.
            </span>
            <span className="font-mono">
              99.1% noise abstention · 98% split-conformal · 35/15 in-silo ratchet
            </span>
          </footer>
        </main>
      </div>
    </div>
  )
}
