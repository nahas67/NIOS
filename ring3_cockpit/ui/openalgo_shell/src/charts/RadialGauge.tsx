/**
 * 270° radial gauge for a bounded score (0..100 signal, 0/30 nursery fills).
 * Pure SVG arc maths; the value is also exposed as `aria-valuenow`.
 */

import type { ReactNode } from "react";

export interface RadialGaugeProps {
  value: number;
  max?: number;
  label: string;
  sublabel?: string;
  size?: number;
  suffix?: string;
  tone?: "accent" | "accent2" | "pos" | "warn" | "neg";
  decimals?: number;
}

const ARC_FRACTION = 0.75;
const START_DEG = 135;

export function RadialGauge({
  value,
  max = 100,
  label,
  sublabel,
  size = 132,
  suffix = "",
  tone = "accent",
  decimals = 0,
}: RadialGaugeProps): ReactNode {
  const safeMax = max <= 0 ? 1 : max;
  const ratio = Math.max(0, Math.min(1, value / safeMax));
  const stroke = Math.max(7, size * 0.075);
  const radius = size / 2 - stroke;
  const circumference = 2 * Math.PI * radius;
  const arcLength = circumference * ARC_FRACTION;
  const color =
    tone === "pos"
      ? "var(--pos)"
      : tone === "warn"
        ? "var(--warn)"
        : tone === "neg"
          ? "var(--neg)"
          : tone === "accent2"
            ? "var(--accent-2)"
            : "var(--accent)";

  return (
    <div
      className="relative inline-flex flex-col items-center"
      role="img"
      aria-label={`${label}: ${value.toFixed(decimals)} of ${safeMax}`}
    >
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="gauge-svg" aria-hidden="true">
        <g transform={`rotate(${START_DEG} ${size / 2} ${size / 2})`}>
          <circle
            className="gauge-track"
            cx={size / 2}
            cy={size / 2}
            r={radius}
            strokeWidth={stroke}
            strokeDasharray={`${arcLength} ${circumference}`}
            strokeLinecap="round"
          />
          <circle
            className="gauge-progress"
            cx={size / 2}
            cy={size / 2}
            r={radius}
            strokeWidth={stroke}
            stroke={color}
            strokeDasharray={`${arcLength * ratio} ${circumference}`}
            style={{ filter: `drop-shadow(0 0 6px ${color})` }}
          />
        </g>
        <text x="50%" y="49%" textAnchor="middle" className="mono tnum" fill="var(--ink)" style={{ fontSize: size * 0.19, fontWeight: 600 }}>
          {value.toFixed(decimals)}
        </text>
        <text x="50%" y="63%" textAnchor="middle" fill="var(--dim)" style={{ fontSize: size * 0.075, letterSpacing: "0.1em" }}>
          / {safeMax}
          {suffix}
        </text>
      </svg>
      <p className="mono -mt-1 text-center text-[9px] tracking-[0.12em] text-[var(--muted)] uppercase">{label}</p>
      {sublabel ? <p className="mt-0.5 max-w-[150px] text-center text-[10px] text-[var(--dim)]">{sublabel}</p> : null}
    </div>
  );
}
