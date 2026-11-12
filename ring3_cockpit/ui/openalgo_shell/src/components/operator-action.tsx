import type { ReactNode } from 'react'

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

interface ActionPanelProps {
  title: string
  description?: string
  children: ReactNode
}

/** Shared frame for an interactive operator control inside a cockpit page. */
export function ActionPanel({ title, description, children }: ActionPanelProps) {
  return (
    <Card className="gap-3 border-border/70 bg-card/60 py-4 shadow-none">
      <CardHeader className="gap-1 px-4">
        <CardTitle className="text-sm">{title}</CardTitle>
        {description ? (
          <p className="text-[11px] text-muted-foreground">{description}</p>
        ) : null}
      </CardHeader>
      <CardContent className="px-4">{children}</CardContent>
    </Card>
  )
}

interface ActionFeedbackProps {
  pending: boolean
  error: string | null
  children?: ReactNode
}

/** Pending / failure line shared by every operator action. */
export function ActionFeedback({ pending, error, children }: ActionFeedbackProps) {
  return (
    <>
      {pending ? (
        <p className="font-mono text-[11px] text-muted-foreground">Executing operator action…</p>
      ) : null}
      {error ? (
        <p
          role="alert"
          className="rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 font-mono text-[11px] text-destructive"
        >
          {error}
        </p>
      ) : null}
      {children}
    </>
  )
}

interface ProofRowProps {
  label: string
  value: string
  mono?: boolean
}

/** Compact label/value pair used by every proof receipt. */
export function ProofRow({ label, value, mono = true }: ProofRowProps) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border/30 py-1 last:border-0">
      <span className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</span>
      <span className={mono ? 'font-mono text-[11px] tabular-nums' : 'text-[11px]'}>{value}</span>
    </div>
  )
}
