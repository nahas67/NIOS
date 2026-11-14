/**
 * Thin line icon set.
 *
 * Every glyph is a 1.5px monochrome stroke on a 24-unit grid. They inherit
 * `currentColor`, so the sidebar can mute them at rest and lift the active item
 * to accent orange without a second asset.
 */

import type { ReactNode } from "react";
import type { IconKey } from "./types";

const PATHS: Record<IconKey, string[]> = {
  dashboard: ["M4 4h7v7H4z", "M13 4h7v4h-7z", "M13 10h7v10h-7z", "M4 13h7v7H4z"],
  terminal: ["M3 20h18", "M5 16l4-5 3 3 4-6 3 4"],
  orders: ["M4 6h16", "M4 12h16", "M4 18h10", "M18 17v4", "M16 19h4"],
  options: ["M4 19l5-6 4 3 6-9", "M4 4v15h16"],
  analyzer: ["M12 4a8 8 0 1 0 0 16a8 8 0 0 0 0-16z", "M12 9a3 3 0 1 0 0 6a3 3 0 0 0 0-6z", "M12 2v3", "M12 19v3"],
  quant: ["M4 18c4-1 5-12 9-12s3 8 7 8", "M4 6h4", "M16 18h4"],
  agents: ["M12 4v4", "M6 20v-4a3 3 0 0 1 6 0v4", "M12 20v-4a3 3 0 0 1 6 0v4", "M12 4a2 2 0 1 0 0 4a2 2 0 0 0 0-4z"],
  backtest: ["M12 4a8 8 0 1 0 8 8", "M12 8v4l3 2", "M17 3v4h-4"],
  brain: ["M9 6a3 3 0 0 0-3 3a3 3 0 0 0 0 6a3 3 0 0 0 3 3V6z", "M15 6a3 3 0 0 1 3 3a3 3 0 0 1 0 6a3 3 0 0 1-3 3V6z", "M12 6v12"],
  forge: ["M4 20l7-7", "M10 4l10 10-3 3L7 7z", "M13 7l4 4"],
  ledger: ["M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3z", "M8 4v16", "M11 9h5", "M11 13h5"],
  logbook: ["M6 4h12v16H6z", "M9 4v16", "M12 8h4", "M12 12h4"],
  verification: ["M12 3l7 3v6c0 4-3 7-7 9c-4-2-7-5-7-9V6z", "M9 12l2 2 4-4"],
  brokers: ["M3 20h18", "M5 20V10", "M9 20V10", "M15 20V10", "M19 20V10", "M3 10l9-6l9 6z"],
  control: ["M4 7h10", "M18 7h2", "M16 5v4", "M4 17h4", "M12 17h8", "M10 15v4"],
  collapse: ["M15 6l-6 6l6 6"],
  expand: ["M9 6l6 6l-6 6"],
  logout: ["M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3", "M10 8l-4 4l4 4", "M6 12h9"],
  shield: ["M12 3l7 3v6c0 4-3 7-7 9c-4-2-7-5-7-9V6z"],
  coin: ["M12 4a8 4 0 1 0 0 8a8 4 0 0 0 0-8z", "M4 8v6a8 4 0 0 0 16 0V8"],
};

export interface GlyphProps {
  name: IconKey;
  size?: number;
  className?: string;
}

export function Glyph({ name, size = 16, className }: GlyphProps): ReactNode {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name].map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}
