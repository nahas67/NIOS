import { useState } from 'react'
import {
  AlertTriangle,
  KeyRound,
  Lock,
  Play,
  RotateCcw,
  ShieldCheck,
  Terminal,
} from 'lucide-react'

import { ControlRoomTab } from '@/components/control-room-tab'
import { PageShell, StatTile } from '@/components/page-shell'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { usePageData } from '@/hooks/use-page-data'
import { callMcpTool, fetchMcpPage } from '@/lib/api'
import { authApi } from '@/lib/auth'
import { cell, num } from '@/lib/rows'
import type { CockpitState } from '@/types'

interface ControlPageProps {
  state: CockpitState | null
  onSaved: () => Promise<void>
}

export function ControlPage({ state, onSaved }: ControlPageProps) {
  const { data, error, loading } = usePageData(fetchMcpPage)
  const tools = data?.tools ?? []
  const [selected, setSelected] = useState('')
  const [args, setArgs] = useState('{}')
  const [output, setOutput] = useState<string>('')
  const [running, setRunning] = useState(false)
  const activeTool = selected || String(tools[0]?.name ?? '')
  const [resetPassword, setResetPassword] = useState('')
  const [resetPin, setResetPin] = useState('')
  const [confirmReset, setConfirmReset] = useState(false)
  const [resetting, setResetting] = useState(false)
  const [resetError, setResetError] = useState<string | null>(null)

  async function factoryReset() {
    if (!confirmReset) {
      setConfirmReset(true)
      setResetError(null)
      return
    }
    if (!resetPassword || !resetPin) {
      setResetError('Password and live PIN are both required.')
      return
    }
    setResetting(true)
    setResetError(null)
    try {
      await authApi.resetEverything({ password: resetPassword, live_pin: resetPin })
      setResetPassword('')
      setResetPin('')
      setConfirmReset(false)
      if (typeof window !== 'undefined') window.location.assign('/setup')
    } catch (cause) {
      setConfirmReset(false)
      setResetError(cause instanceof Error ? cause.message : 'factory reset failed')
    } finally {
      setResetting(false)
    }
  }

  function cancelFactoryReset() {
    setConfirmReset(false)
    setResetPassword('')
    setResetPin('')
    setResetError(null)
  }

  async function runTool() {
    if (!activeTool) return
    setRunning(true)
    setOutput('')
    let parsed: Record<string, unknown> = {}
    try {
      const candidate = JSON.parse(args || '{}')
      if (candidate && typeof candidate === 'object' && !Array.isArray(candidate)) {
        parsed = candidate as Record<string, unknown>
      }
    } catch {
      setRunning(false)
      setOutput('Arguments must be a JSON object.')
      return
    }
    try {
      const receipt = await callMcpTool(activeTool, parsed)
      setOutput(JSON.stringify(receipt, null, 2))
    } catch (err) {
      setOutput(err instanceof Error ? err.message : 'tool call failed')
    } finally {
      setRunning(false)
    }
  }

  return (
    <PageShell
      title="Control Room & MCP Console"
      ring="RING 3"
      subtitle="22-gate pre-flight certificate, write-only broker credential vault, Ring 0 key rotation, the LLM switchboard and the air-gapped read-only MCP bridge."
      actions={
        <Badge variant="outline" className="font-mono text-[10px]">
          {state?.system?.status ?? 'ARMED'}
        </Badge>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          icon={ShieldCheck}
          label="MCP tools"
          value={`${data?.count ?? 0}`}
          hint={data?.transport ?? 'in-process read-only bridge'}
        />
        <StatTile
          icon={Lock}
          label="Order-signing tools"
          value={`${data?.invariants?.order_signing_tools ?? 0}`}
          hint="The bridge can never sign or trade"
        />
        <StatTile
          icon={Terminal}
          label="Console ready"
          value={data?.console_ready ? 'YES' : 'NO'}
          hint={`${tools.length} callable read-only tools`}
        />
        <StatTile
          icon={ShieldCheck}
          label="Air gap"
          value={data?.air_gap?.mcp_air_gap_pass ? 'PASS' : 'CHECK'}
          hint="Ring 3 stays read-only for market state"
        />
      </div>

      <Card className="border-border/70 bg-card/60 shadow-none">
        <CardHeader className="px-4">
          <CardTitle className="text-sm">Air-gapped MCP console</CardTitle>
          <p className="text-[11px] text-muted-foreground">
            POST <span className="font-mono">/api/v2/mcp/call</span> — read-only tools only. No tool
            can read broker keys or place an order.
          </p>
        </CardHeader>
        <CardContent className="space-y-3 px-4">
          {loading ? (
            <p className="text-[11px] text-muted-foreground">Loading tool inventory…</p>
          ) : null}
          {error ? <p className="text-[11px] text-amber-400">{error}</p> : null}
          {tools.length === 0 && !loading ? (
            <p className="text-[11px] text-muted-foreground">
              The MCP bridge reported no enabled tools.
            </p>
          ) : null}
          <div className="flex flex-wrap gap-1.5">
            {tools.map((tool) => {
              const name = String(tool.name ?? '')
              const active = name === activeTool
              return (
                <Button
                  key={name}
                  type="button"
                  size="sm"
                  variant={active ? 'secondary' : 'outline'}
                  className="h-7 font-mono text-[10px]"
                  onClick={() => setSelected(name)}
                >
                  {name}
                </Button>
              )
            })}
          </div>
          <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
            <Input
              value={args}
              onChange={(event) => setArgs(event.target.value)}
              spellCheck={false}
              className="h-8 font-mono text-[11px]"
              aria-label="Tool arguments JSON"
              placeholder='{"silo": "CRYPTO_BINANCE"}'
            />
            <Button
              type="button"
              size="sm"
              className="h-8 font-mono text-[10px]"
              disabled={running || !activeTool}
              onClick={() => void runTool()}
            >
              <Play className="mr-1.5 size-3" />
              {running ? 'RUNNING' : 'CALL TOOL'}
            </Button>
          </div>
          <ScrollArea className="h-48 rounded-lg border border-border/50 bg-background/60 p-3">
            <pre className="whitespace-pre-wrap font-mono text-[10px] leading-relaxed text-muted-foreground">
              {output || 'Select a tool, pass JSON arguments and run it. Receipts are proof-only.'}
            </pre>
          </ScrollArea>
          <div className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-[10px] text-muted-foreground">
            <span>ring3_read_only={cell({ value: data?.air_gap?.ring3_read_only ?? true }, 'value')}</span>
            <span>venues_tradable={num(data?.invariants ?? {}, 'venues_tradable')}</span>
            <span>key_reading_tools={num(data?.invariants ?? {}, 'key_reading_tools')}</span>
          </div>
        </CardContent>
      </Card>

      <Card className="border-destructive/40 bg-destructive/5 shadow-none">
        <CardHeader className="px-4">
          <CardTitle className="flex items-center gap-2 text-sm">
            <KeyRound className="size-3.5" />
            Sovereign factory reset
          </CardTitle>
          <p className="text-[11px] text-muted-foreground">
            POST <span className="font-mono">/api/v2/auth/reset</span> — deletes the operator
            credential and zeroes every value. Once an operator exists the Ring 0 vault refuses
            the reset unless the password and the live PIN both match their stored digests.
          </p>
        </CardHeader>
        <CardContent className="space-y-3 px-4">
          <div className="grid gap-2 sm:grid-cols-2">
            <div className="space-y-1">
              <label
                className="font-mono text-[10px] text-muted-foreground"
                htmlFor="factory-reset-password"
              >
                password
              </label>
              <Input
                id="factory-reset-password"
                type="password"
                value={resetPassword}
                onChange={(event) => setResetPassword(event.target.value)}
                className="h-8 font-mono text-[11px]"
                placeholder="operator password"
                autoComplete="current-password"
                disabled={resetting}
              />
            </div>
            <div className="space-y-1">
              <label className="font-mono text-[10px] text-muted-foreground" htmlFor="factory-reset-pin">
                live_pin
              </label>
              <Input
                id="factory-reset-pin"
                type="password"
                inputMode="numeric"
                value={resetPin}
                onChange={(event) => setResetPin(event.target.value)}
                className="h-8 font-mono text-[11px]"
                placeholder="4-12 digits"
                autoComplete="off"
                disabled={resetting}
              />
            </div>
          </div>
          {resetError ? (
            <p className="flex items-center gap-1.5 text-[11px] text-amber-300">
              <AlertTriangle className="size-3 shrink-0" />
              {resetError}
            </p>
          ) : null}
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              size="sm"
              variant={confirmReset ? 'destructive' : 'outline'}
              className="h-8 text-[10px]"
              disabled={resetting}
              onClick={() => void factoryReset()}
            >
              <RotateCcw className="mr-1.5 size-3" />
              {confirmReset ? 'CONFIRM FACTORY RESET' : 'REQUEST FACTORY RESET'}
            </Button>
            {confirmReset ? (
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="h-8 text-[10px]"
                disabled={resetting}
                onClick={cancelFactoryReset}
              >
                CANCEL
              </Button>
            ) : null}
            <span className="font-mono text-[10px] text-muted-foreground">
              401 AUTH_REQUIRED · 403 INVALID_CREDENTIALS · 200 clean slate
            </span>
          </div>
        </CardContent>
      </Card>

      <ControlRoomTab state={state} onSaved={onSaved} />
    </PageShell>
  )
}
