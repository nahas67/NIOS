import { useCallback, useEffect, useState } from 'react'

import { fetchState, postKillSwitch, postRearm, streamUrl } from '@/lib/api'
import type {
  CockpitCard,
  CockpitFrame,
  CockpitState,
  KillReport,
  RearmReport,
  SiloState,
} from '@/types'

const MAX_CARDS = 120

function cardKey(card: CockpitCard): string {
  return `${card.timestamp_ns}:${card.silo}:${card.symbol}:${card.mode_badge}:${card.proof_line}`
}

function mergeCards(previous: CockpitCard[], incoming: CockpitCard[]): CockpitCard[] {
  if (incoming.length === 0) return previous
  const seen = new Set(previous.map(cardKey))
  const fresh = incoming.filter((card) => !seen.has(cardKey(card)))
  if (fresh.length === 0) return previous
  return [...fresh.reverse(), ...previous].slice(0, MAX_CARDS)
}

function mergeSilos(
  previous: Record<string, SiloState>,
  frame: CockpitFrame
): Record<string, SiloState> {
  const next: Record<string, SiloState> = { ...previous }
  for (const [siloId, payload] of Object.entries(frame.silos)) {
    const existing = previous[siloId]
    if (existing) {
      next[siloId] = { ...existing, vaults: payload.vaults, mode: payload.mode as SiloState['mode'] }
    }
  }
  return next
}

export interface CockpitController {
  state: CockpitState | null
  cards: CockpitCard[]
  connected: boolean
  error: string | null
  killReport: KillReport | null
  rearmReport: RearmReport | null
  lastFrameAt: number
  refresh: () => Promise<void>
  kill: () => Promise<void>
  rearm: () => Promise<void>
  dismissError: () => void
}

export function useCockpit(): CockpitController {
  const [state, setState] = useState<CockpitState | null>(null)
  const [cards, setCards] = useState<CockpitCard[]>([])
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [killReport, setKillReport] = useState<KillReport | null>(null)
  const [rearmReport, setRearmReport] = useState<RearmReport | null>(null)
  const [lastFrameAt, setLastFrameAt] = useState(0)

  const refresh = useCallback(async () => {
    try {
      const next = await fetchState()
      setState(next)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Ring 3 gateway unreachable')
    }
  }, [])

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => {
      void refresh()
    }, 5_000)
    return () => window.clearInterval(timer)
  }, [refresh])

  useEffect(() => {
    const source = new EventSource(streamUrl())
    const onOpen = () => setConnected(true)
    const onFrame = (event: MessageEvent<string>) => {
      try {
        const frame = JSON.parse(event.data) as CockpitFrame
        if (frame.type !== 'cockpit_frame') return
        setConnected(true)
        setLastFrameAt(Date.now())
        setCards((previous) => mergeCards(previous, frame.cards ?? []))
        setState((previous) =>
          previous
            ? {
                ...previous,
                system: frame.system,
                abstention: frame.abstention,
                silos: mergeSilos(previous.silos, frame),
              }
            : previous
        )
      } catch {
        // A malformed frame is dropped; the next 16 ms frame supersedes it.
      }
    }
    const onError = () => setConnected(false)

    source.addEventListener('open', onOpen)
    source.addEventListener('cockpit', onFrame as EventListener)
    source.addEventListener('error', onError)
    return () => {
      source.removeEventListener('open', onOpen)
      source.removeEventListener('cockpit', onFrame as EventListener)
      source.removeEventListener('error', onError)
      source.close()
    }
  }, [])

  const kill = useCallback(async () => {
    try {
      const report = await postKillSwitch('cockpit 1-click kill switch')
      setKillReport(report)
      setRearmReport(null)
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'kill switch failed')
    }
  }, [refresh])

  const rearm = useCallback(async () => {
    try {
      const report = await postRearm('cockpit re-arm')
      setRearmReport(report)
      setKillReport(null)
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 're-arm failed')
    }
  }, [refresh])

  return {
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
    dismissError: () => setError(null),
  }
}
