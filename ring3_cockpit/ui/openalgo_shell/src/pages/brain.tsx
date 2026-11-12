import { BrainCircuit, Radio, ShieldCheck } from 'lucide-react'

import { BrainXrayTab } from '@/components/brain-xray-tab'
import { PageShell, StatTile } from '@/components/page-shell'
import { Badge } from '@/components/ui/badge'
import type { CockpitState } from '@/types'

interface BrainPageProps {
  state: CockpitState | null
}

export function BrainPage({ state }: BrainPageProps) {
  const processes = state?.ring_processes
  const contagion = state?.contagion

  return (
    <PageShell
      title="Brain & Contagion X-Ray"
      ring="RING 2"
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

      <BrainXrayTab state={state} />
    </PageShell>
  )
}
