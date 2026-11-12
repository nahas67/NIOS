import { AlertTriangle, X } from 'lucide-react'

import { AppSidebar } from '@/components/app-sidebar'
import { TopBar } from '@/components/top-bar'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useCockpit } from '@/hooks/use-cockpit'
import { AnalyzerPage } from '@/pages/analyzer'
import { BrainPage } from '@/pages/brain'
import { ControlPage } from '@/pages/control'
import { DashboardPage } from '@/pages/dashboard'
import { ForgePage } from '@/pages/forge'
import { LedgerPage } from '@/pages/ledger'
import { OrdersPage } from '@/pages/orders'
import { QuantPage } from '@/pages/quant'
import { VerificationPage } from '@/pages/verification'
import { legacyTabFor, pathForLegacyTab, useRoute } from '@/router'
import type { LegacyTab } from '@/router'

export function App() {
  const cockpit = useCockpit()
  const route = useRoute()
  const {
    state,
    cards,
    connected,
    error,
    killReport,
    rearmReport,
    lastFrameAt,
    refresh,
    kill,
    rearm,
  } = cockpit

  const legacyTab = legacyTabFor(route.page)

  function renderPage() {
    switch (route.page) {
      case 'orders':
        return <OrdersPage />
      case 'analyzer':
        return <AnalyzerPage />
      case 'quant':
        return <QuantPage />
      case 'brain':
        return <BrainPage state={state} />
      case 'forge':
        return <ForgePage />
      case 'ledger':
        return <LedgerPage />
      case 'verification':
        return <VerificationPage state={state} />
      case 'control':
        return <ControlPage state={state} onSaved={refresh} />
      default:
        return <DashboardPage state={state} cards={cards} />
    }
  }

  return (
    <div className="dark">
      <div className="relative min-h-screen overflow-hidden bg-background text-foreground">
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 opacity-70"
          style={{
            backgroundImage:
              'radial-gradient(60rem 30rem at 12% 0%, rgba(56,189,248,0.10), transparent 70%), radial-gradient(50rem 26rem at 88% 8%, rgba(168,85,247,0.10), transparent 70%), linear-gradient(rgba(148,163,184,0.05) 1px, transparent 1px), linear-gradient(90deg, rgba(148,163,184,0.05) 1px, transparent 1px)',
            backgroundSize: 'auto, auto, 48px 48px, 48px 48px',
          }}
        />

        <div className="relative z-10 flex min-h-screen">
          <AppSidebar
            page={route.page}
            onNavigate={route.go}
            state={state}
            connected={connected}
          />

          <div className="flex min-w-0 flex-1 flex-col">
            <TopBar
              state={state}
              connected={connected}
              killReport={killReport}
              rearmReport={rearmReport}
              lastFrameAt={lastFrameAt}
              onKill={kill}
              onRearm={rearm}
              onFeedChanged={refresh}
            />

            {error ? (
              <div className="mx-4 mt-3 flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200 lg:mx-6">
                <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
                <div className="flex-1">
                  Ring 3 gateway: {error}. The cockpit will retry automatically.
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-5"
                  onClick={cockpit.dismissError}
                  aria-label="Dismiss gateway error"
                >
                  <X className="size-3" />
                </Button>
              </div>
            ) : null}

            <main className="relative flex-1 px-4 pb-10 pt-4 lg:px-6">
              {legacyTab ? (
                <Tabs
                  value={legacyTab}
                  onValueChange={(next) => route.go(pathForLegacyTab(next as LegacyTab))}
                  className="gap-4"
                >
                  <TabsList className="grid h-10 w-full max-w-[560px] grid-cols-3">
                    <TabsTrigger value="live">Live Cockpit</TabsTrigger>
                    <TabsTrigger value="xray">Brain &amp; Contagion</TabsTrigger>
                    <TabsTrigger value="control">Control Room</TabsTrigger>
                  </TabsList>
                  <TabsContent value={legacyTab}>{renderPage()}</TabsContent>
                </Tabs>
              ) : (
                renderPage()
              )}
            </main>

            <footer className="relative z-10 border-t border-border/50 px-4 py-3 text-[10px] text-muted-foreground lg:px-6">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span>
                  NIOS-ARC-2026-V3 · v{state?.version ?? '3.0.0-PROD'} · Ring 3 is read-only for
                  market state · 9 production pages
                </span>
                <span>
                  99.1% noise abstention · 98% split-conformal · 35/15 in-silo ratchet
                </span>
              </div>
            </footer>
          </div>
        </div>
      </div>
    </div>
  )
}

export default App
