/**
 * Flat SVG chart kit.
 *
 * One geometry helper, one frame, one legend. Every visualisation in the
 * terminal draws with a 1px hairline grid on the module surface and never with
 * a gradient fill, so a printed screenshot stays legible and a retina display
 * shows a crisp line rather than a bloom.
 */

import type { ReactNode } from "react";

export const CHART = {
  grid: "#27292E",
  axis: "#5A5E66",
  text: "#8C9099",
  strong: "#F4F5F7",
  surface: "#16181C",
  canvas: "#0D0E10",
  raised: "#22242A",
  accent: "#F96316",
  bull: "#38B27B",
  bullFill: "#172B23",
  bullLine: "#234235",
  bear: "#E05555",
  bearFill: "#2D191A",
  bearLine: "#472527",
} as const;

export function scale(domainMin: number, domainMax: number, size: number): (value: number) => number {
  const span = domainMax - domainMin;
  if (!Number.isFinite(span) || span === 0) return () => size / 2;
  return (value: number) => ((value - domainMin) / span) * size;
}

export function linePath(points: Array<[number, number]>): string {
  if (points.length === 0) return "";
  return points
    .map(([x, y], index) => `${index === 0 ? "M" : "L"}${x.toFixed(2)} ${y.toFixed(2)}`)
    .join(" ");
}

export function areaPath(points: Array<[number, number]>, baseline: number): string {
  if (points.length === 0) return "";
  const first = points[0];
  const last = points[points.length - 1];
  return `${linePath(points)} L${last[0].toFixed(2)} ${baseline.toFixed(2)} L${first[0].toFixed(2)} ${baseline.toFixed(2)} Z`;
}

export function extent(values: number[], fallback: [number, number] = [0, 1]): [number, number] {
  const clean = values.filter((value) => Number.isFinite(value));
  if (clean.length === 0) return fallback;
  let min = clean[0];
  let max = clean[0];
  for (const value of clean) {
    if (value < min) min = value;
    if (value > max) max = value;
  }
  if (min === max) return [min - 1, max + 1];
  return [min, max];
}

export interface ChartLegendItem {
  label: string;
  color: string;
  dashed?: boolean;
}

export function ChartLegend({ items }: { items: ChartLegendItem[] }): ReactNode {
  return (
    <div className="nx-chart-legend">
      {items.map((item) => (
        <span key={item.label} className="nx-chart-key">
          <i
            className="nx-chart-swatch"
            aria-hidden="true"
            style={{
              background: item.dashed ? "transparent" : item.color,
              borderTop: item.dashed ? `1px dashed ${item.color}` : undefined,
            }}
          />
          {item.label}
        </span>
      ))}
    </div>
  );
}

export interface ChartFrameProps {
  label: string;
  legend?: ChartLegendItem[];
  empty?: string;
  children?: ReactNode;
  /** Plot rectangle in viewBox units. */
  box: { x: number; y: number; w: number; h: number };
  viewBox: string;
  height: number;
  action?: ReactNode;
}

/** Grid, axis baseline and the empty-state text shared by every chart. */
export function plotGrid(box: { x: number; y: number; w: number; h: number }, rows = 4, cols = 6) {
  const lines = [];
  for (let row = 0; row <= rows; row += 1) {
    const y = box.y + (box.h / rows) * row;
    lines.push(
      <line
        key={`r${row}`}
        x1={box.x}
        y1={y}
        x2={box.x + box.w}
        y2={y}
        stroke={CHART.grid}
        strokeWidth={1}
        shapeRendering="crispEdges"
      />,
    );
  }
  for (let col = 0; col <= cols; col += 1) {
    const x = box.x + (box.w / cols) * col;
    lines.push(
      <line
        key={`c${col}`}
        x1={x}
        y1={box.y}
        x2={x}
        y2={box.y + box.h}
        stroke={CHART.grid}
        strokeWidth={1}
        shapeRendering="crispEdges"
        opacity={col === 0 || col === cols ? 1 : 0.5}
      />,
    );
  }
  return lines;
}
