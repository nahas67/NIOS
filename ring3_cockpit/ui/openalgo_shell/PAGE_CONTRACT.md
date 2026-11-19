# NIOS V2.2 — Workstation Page Contract (authoritative)

Every file in `src/pages/` MUST conform to this contract. A build failure in ANY page
breaks the whole release, so follow it exactly.

## 1. Module shape

```tsx
import { useCallback, useEffect, useMemo, useState } from "react";
import type { PageProps } from "./types";
// ... other imports

export default function ExamplePage({ mode }: PageProps) {
  // hooks
  return (
    <div className="flex flex-col gap-3">
      <PageHeader title="..." subtitle="..." />
      {/* sections */}
    </div>
  );
}
```

- Default export, function component, named exactly `<Name>Page`.
- `PageProps = { mode: "PAPER" | "LIVE" }` from `./types`.
- React 19. No `any`, no `@ts-ignore`, no `as unknown as T` casts of payloads.
- **Zero unused imports/variables** (`biome lint` treats them as errors).

## 2. Available imports (all already exist — do not create or edit them)

```ts
import { Icon } from "../components/Icon";                 // IconName union of 55 names
import {
  Badge, Banner, Button, Card, Cell, Checkbox, Divider, EmptyState, Field, JsonBlock,
  KeyValue, Modal, PageHeader, ProgressBar, Row, SelectInput, Spinner, Stat, StatGrid,
  StatusDot, Table, Tabs, TextArea, TextInput, Toolbar, cx,
} from "../components/ui";
import {
  BarChart, CandlestickChart, ChartLegend, DepthLadder, EquityDrawdownCurve, Heatmap,
  LineChart, PayoffCurveChart, Sparkline,
} from "../charts";
import {
  MICROS, asArray, asRecord, asRows, bps, clockNs, compact, dateUtc, deepGet, duration,
  int, isRecord, namespaceLabel, num, pct, pick, priceMicros, qty, safeBool, safeNumber,
  shortHash, signed, signedToneClass, text, timeUtc, titleize, toneClass, toneForNumber,
  units, unitsSigned, upper, usd, usdSigned, nowNs,
} from "../lib/format";
import {
  ApiError, LIMITS, api, assertFinite, authLogout, clampBars, clampLimit, clampSagas,
  errorMessage, fetchAlerts, getCsrfToken, getSessionToken, isCriticalAlert, setTokens,
} from "../lib/api";
import { preflightOf, rowsOf, gatesOf, statusTone, navMicros, pnlMicros, drawdownBps } from "../lib/payload";
import { useMarketStream, SSE_TYPES } from "../lib/stream";
import { useSession, useTicker } from "../state/session";
import { SILOS, VAULTS, RING_LEGEND, WORKSTATIONS } from "../config/nav";
```

Exact signatures you may rely on:

- `api.get<T>(path: string, params?: Record<string, string|number|boolean|undefined>, signal?: AbortSignal): Promise<T>`
- `api.post<T>(path: string, body?: unknown, params?: ...): Promise<T>`
- `api.patch<T>`, `api.del<T>`
- Paths are **relative to `/api/v2`** — e.g. `api.get("/missions")`, `api.post("/control/autopilot/tick", {})`.
- All mutating calls already attach `X-NIOS-Session` + `X-NIOS-CSRF` automatically. Never build `fetch()` yourself.
- `useMarketStream({ topics?: string[]; silo?: string; max?: number })` →
  `{ frames: StreamFrame[]; status: "idle"|"connecting"|"open"|"error"; error: string | null; clear: () => void }`
  where `StreamFrame = { id: string; type: string; topic: string; receivedAt: number; data: Record<string, unknown> }`.
- `useSession()` → `{ session, loading, error, lastSync, alerts, unacknowledged, critical, refresh, refreshAlerts }`
- `useTicker(ms?: number): number` — a ticking `Date.now()` for TTL displays.
- `useTicker` and `useSessionTtl` are the only timers you need; do **not** write `setInterval` for clocks.

## 3. Visual law (violating this fails Suite 38)

- Surfaces: `bg-canvas` (#0D0E10), `bg-surface` (#16181C), `bg-elevated` (#22242A).
- Hairlines: `border border-hairline` (#27292E), always 1px.
- Accent: `text-accent` / `bg-accent` / `border-accent` (#F96316) — sparse, active states only.
- Text: `text-ink` (#F4F5F7), `text-ink-2` (#8C9099), `text-ink-3` (#5A5E66).
- Bull: `text-bull` `bg-bull-tint` `border-bull-border`. Bear: `text-bear` `bg-bear-tint` `border-bear-border`.
- Radii **only**: `rounded-[2px]`, `rounded-[4px]`, `rounded-[6px]`, `rounded-[8px]`.
- Numbers: always `tabular-nums` (add `tabular-nums` class or use `nios-num`).
- FORBIDDEN tokens anywhere in source: `rounded-full`, `rounded-xl`, `rounded-2xl`, `rounded-3xl`,
  `shadow`, `backdrop-blur`, `backdrop-filter`, `gradient`, `blur(`, `9999px`, `999px`.
- No inline `style` with `boxShadow`, `filter`, or gradients. Widths/heights via props or classes.
- Icons: `<Icon name="..." size={14} className="text-ink-2" />`, 1.5px stroke, monochrome.
- Density: prefer `text-[11px] uppercase tracking-[0.08em] text-ink-3` for column headers,
  `text-[13px]` body, `text-[15px] font-semibold` for primary figures.

## 4. Data discipline

- Every API call runs inside `useEffect` (or an event handler) with `try/catch`; store
  `rows`, `payload`, `loading`, `error`.
- Use a local `const [note, setNote] = useState<{tone:"neutral"|"bull"|"bear"; message:string}|null>(null)`
  and render `<Banner tone={note.tone}>{note.message}</Banner>` for action results.
- Never assume a payload shape. Use `rowsOf(payload)` / `asArray` / `safeNumber` / `text`
  so a partial backend response still renders.
- Non-finite or out-of-range numeric inputs must be rejected client-side with
  `assertFinite(value, "label")` **before** calling the API.
- Clamp list limits: `clampLimit(v, 1, 5000, 500)` for bars, `clampSagas(v)` for sagas,
  `clampBars(v, 240)` for backtests.
- Show a `<Spinner />` while `loading`, `<EmptyState>` when a list is empty, and a red
  `<Banner tone="bear">{error}</Banner>` on failure. A page must never crash on `{}`.

## 5. Page skeleton (use this shape)

```tsx
<div className="flex flex-col gap-3">
  <PageHeader
    title="MISSIONS"
    subtitle="Multi-tranche mission, profit-split saga & autopilot orchestrator"
    badges={<Badge tone="accent" mono>PAPER</Badge>}
    actions={<Button icon="refresh" variant="subtle" size="sm" onClick={load}>Refresh</Button>}
  />
  {error ? <Banner tone="bear">{error}</Banner> : null}
  {note ? <Banner tone={note.tone}>{note.message}</Banner> : null}
  <StatGrid columns={4}>{/* <Stat .../> x4 */}</StatGrid>
  <Card title="..." subtitle="...">{/* Table / chart */}</Card>
</div>
```

## 6. Route wiring already done

`src/App.tsx` imports every page as `import XPage from "./pages/XPage"` and renders it
inside `PaperShell` / `LiveShell`. Do not touch `App.tsx`, `main.tsx`, shells, portals,
`lib/`, `components/`, `charts/`, `config/`, or `state/`.
