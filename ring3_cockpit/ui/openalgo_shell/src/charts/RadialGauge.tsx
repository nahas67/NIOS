/**
 * Segmented radial gauge.
 *
 * A 240-degree arc on a raised track, cut into 40 flat segments so the value is
 * legible at a glance without a sweep animation or an accent bloom. The filled
 * run takes the accent for a neutral reading and emerald-sage once the metric
 * clears its healthy band.
 */

import type { ReactNode } from "react";
import { CHART } from "./kit";

export interface RadialGaugeProps {
  value: number;
  max?: number;
  label: string;
  unit?: string;
  segments?: number;
  tone?: "accent" | "bull" | "bear";
  size?: number;
  hint?: string;
}

const START = 150;
const SWEEP = 240;

function polar(cx: number, cy: number, radius: number, angleDeg: number): [number, number] {
  const angle = (angleDeg * Math.PI) / 180;
  return [cx + radius * Math.cos(angle), cy + radius * Math.sin(angle)];
}

function arcPath(cx: number, cy: number, radius: number, from: number, to: number): string {
  const [x1, y1] = polar(cx, cy, radius, from);
  const [x2, y2] = polar(cx, cy, radius, to);
  const large = Math.abs(to - from) > 180 ? 1 : 0;
  return `M${x1.toFixed(2)} ${y1.toFixed(2)} A${radius} ${radius} 0 ${large} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
}

export function RadialGauge({
  value,
  max = 100,
  label,
  unit = "%",
  segments = 40,
  tone = "accent",
  size = 190,
  hint,
}: RadialGaugeProps): ReactNode {
  const safeMax = max > 0 ? max : 1;
  const ratio = Math.max(0, Math.min(1, value / safeMax));
  const active = Math.round(ratio * segments);
  const color = tone === "bull" ? CHART.bull : tone === "bear" ? CHART.bear : CHART.accent;
  const cx = size / 2;
  const cy = size / 2;
  const radius = size / 2 - 16;
  const step = SWEEP / segments;

  return (
    <div className="nx-stack" style={{ alignItems: "center" }}>
      <svg
        className="nx-chart"
        viewBox={`0 0 ${size} ${size}`}
        style={{ height: size, width: size }}
        role="img"
        aria-label={`${label}: ${value.toFixed(1)} of ${max}`}
      >
        <title>{label}</title>
        {Array.from({ length: segments }).map((_, index) => {
          const from = START + step * index + 1.1;
          const to = START + step * (index + 1) - 1.1;
          const isActive = index < active;
          return (
            <path
              key={`seg-${index}`}
              d={arcPath(cx, cy, radius, from, to)}
              fill="none"
              stroke={isActive ? color : CHART.raised}
              strokeWidth={9}
              strokeLinecap="butt"
            />
          );
        })}
        <text
          x={cx}
          y={cy - 2}
          textAnchor="middle"
          fill={CHART.strong}
          fontSize={size * 0.16}
          fontWeight={600}
        >
          {value.toFixed(tone === "accent" ? 1 : 0)}
        </text>
        <text x={cx} y={cy + 16} textAnchor="middle" fill={CHART.text} fontSize={size * 0.055}>
          {unit}
        </text>
        <text
          x={cx}
          y={cy + radius * 0.62}
          textAnchor="middle"
          fill={CHART.axis}
          fontSize={size * 0.05}
          letterSpacing="0.12em"
        >
          {label.toUpperCase()}
        </text>
        <circle cx={cx} cy={cy + radius * 0.24} r={1.5} fill={color} />
      </svg>
      {hint ? <span className="nx-xs nx-muted">{hint}</span> : null}
    </div>
  );
}
