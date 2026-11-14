/**
 * Shared chart geometry.
 *
 * Every visualisation in `src/charts` is a hand-built responsive SVG: no chart
 * runtime, no canvas, no layout thrash. These helpers keep the scales, the
 * crosshair maths and the tabular tooltip identical across all six charts.
 */

import { useCallback, useMemo, useState, type PointerEvent, type ReactNode } from "react";

export const VB_W = 720;

export interface Domain {
  min: number;
  max: number;
}

export function domainOf(values: number[], paddingPct = 0.06): Domain {
  const clean = values.filter((value) => Number.isFinite(value));
  if (clean.length === 0) return { min: 0, max: 1 };
  let min = Math.min(...clean);
  let max = Math.max(...clean);
  if (min === max) {
    const bump = Math.abs(min) * 0.02 || 1;
    min -= bump;
    max += bump;
  }
  const pad = (max - min) * paddingPct;
  return { min: min - pad, max: max + pad };
}

export function scale(value: number, domain: Domain, size: number): number {
  const span = domain.max - domain.min || 1;
  return ((value - domain.min) / span) * size;
}

export function invert(value: number, domain: Domain, size: number): number {
  const span = domain.max - domain.min || 1;
  return domain.min + (value / (size || 1)) * span;
}

export function ticks(domain: Domain, count = 4): number[] {
  if (count <= 0) return [];
  return Array.from({ length: count + 1 }, (_, index) => domain.min + ((domain.max - domain.min) * index) / count);
}

export function linePath(points: Array<[number, number]>): string {
  if (points.length === 0) return "";
  return points.map(([x, y], index) => `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
}

export function areaPath(points: Array<[number, number]>, baseline: number): string {
  if (points.length === 0) return "";
  const first = points[0];
  const last = points[points.length - 1];
  return `M${first[0].toFixed(2)},${baseline.toFixed(2)} ${points
    .map(([x, y]) => `L${x.toFixed(2)},${y.toFixed(2)}`)
    .join(" ")} L${last[0].toFixed(2)},${baseline.toFixed(2)} Z`;
}

export interface PointerState {
  index: number | null;
  x: number;
  bind: {
    onPointerMove: (event: PointerEvent<SVGSVGElement>) => void;
    onPointerLeave: () => void;
  };
}

/** Map a pointer position onto an index of `length` samples. */
export function useCrosshair(length: number, vbWidth = VB_W): PointerState {
  const [index, setIndex] = useState<number | null>(null);

  const onPointerMove = useCallback(
    (event: PointerEvent<SVGSVGElement>) => {
      if (length <= 0) return;
      const rect = event.currentTarget.getBoundingClientRect();
      if (rect.width <= 0) return;
      const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
      setIndex(Math.min(length - 1, Math.round(ratio * (length - 1))));
    },
    [length],
  );

  const onPointerLeave = useCallback(() => setIndex(null), []);

  const x = useMemo(() => {
    if (index === null || length <= 1) return 0;
    return (index / (length - 1)) * vbWidth;
  }, [index, length, vbWidth]);

  return { index, x, bind: { onPointerMove, onPointerLeave } };
}

export function ChartTooltip({
  x,
  children,
  width = 196,
}: {
  x: number;
  children: ReactNode;
  width?: number;
}): ReactNode {
  const pct = Math.min(94, Math.max(6, (x / VB_W) * 100));
  return (
    <div
      className="pointer-events-none absolute top-2 z-10 rounded-[var(--r-sm)] border border-[var(--line-strong)] bg-[rgba(6,9,16,0.96)] px-2.5 py-2 shadow-[0_18px_40px_-22px_#000]"
      style={{ left: `${pct}%`, width, transform: "translateX(-50%)" }}
      role="tooltip"
    >
      {children}
    </div>
  );
}

export function TooltipRow({ label, value, tone }: { label: string; value: ReactNode; tone?: string }): ReactNode {
  return (
    <div className="flex items-center justify-between gap-3 text-[10px]">
      <span className="text-[var(--dim)]">{label}</span>
      <span className={`mono tnum ${tone ?? "text-[var(--ink-soft)]"}`}>{value}</span>
    </div>
  );
}

export function GridLines({
  domain,
  plotH,
  plotW,
  left,
  top,
  count = 4,
  format,
}: {
  domain: Domain;
  plotH: number;
  plotW: number;
  left: number;
  top: number;
  count?: number;
  format: (value: number) => string;
}): ReactNode {
  return (
    <g>
      {ticks(domain, count).map((value) => {
        const y = top + plotH - scale(value, domain, plotH);
        return (
          <g key={value}>
            <line x1={left} x2={left + plotW} y1={y} y2={y} stroke="rgba(148,163,184,0.09)" strokeDasharray="2 4" />
            <text x={left - 6} y={y + 3} textAnchor="end" className="iso-ring-label" style={{ fontSize: 8 }}>
              {format(value)}
            </text>
          </g>
        );
      })}
    </g>
  );
}
