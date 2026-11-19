/**
 * Thin monochrome line iconography — 1.5px stroke, no fills, no gradients.
 * Icons inherit `currentColor` so a muted gray can become off-white or burnt
 * orange purely through state.
 */

export type IconName =
  | "grid"
  | "target"
  | "screener"
  | "candles"
  | "orders"
  | "options"
  | "analyzer"
  | "sigma"
  | "agents"
  | "backtest"
  | "brain"
  | "forge"
  | "ledger"
  | "logbook"
  | "verification"
  | "brokers"
  | "control"
  | "bell"
  | "power"
  | "lock"
  | "unlock"
  | "play"
  | "pause"
  | "arrowUp"
  | "arrowDown"
  | "arrowRight"
  | "chevronDown"
  | "chevronRight"
  | "chevronLeft"
  | "check"
  | "close"
  | "refresh"
  | "plus"
  | "minus"
  | "external"
  | "clock"
  | "database"
  | "activity"
  | "zap"
  | "key"
  | "server"
  | "wifi"
  | "warning"
  | "info"
  | "search"
  | "terminal"
  | "shield"
  | "download"
  | "filter"
  | "layer"
  | "eye"
  | "flame"
  | "pin";

const PATHS: Record<IconName, string> = {
  grid: "M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z",
  target: "M12 3v3M12 18v3M3 12h3M18 12h3M12 7a5 5 0 1 0 0 10a5 5 0 0 0 0-10M12 10a2 2 0 1 0 0 4a2 2 0 0 0 0-4",
  screener: "M3 5h18M3 12h12M3 19h6M17 15l4 4M21 15l-4 4",
  candles: "M6 4v4M6 16v4M6 8h0M8 8h-4v8h4zM15 3v5M15 17v4M17 8h-4v9h4zM21 6v3M21 15v5",
  orders: "M4 6h16M4 12h16M4 18h10M18 16l2 2l2-2",
  options: "M12 3l9 5l-9 5l-9-5zM3 12l9 5l9-5M3 16l9 5l9-5",
  analyzer: "M2 12s4-7 10-7s10 7 10 7s-4 7-10 7S2 12 2 12M12 9a3 3 0 1 0 0 6a3 3 0 0 0 0-6",
  sigma: "M18 4H6l6 8l-6 8h12",
  agents: "M9 3h6v3H9zM4 9h16v9H4zM9 13h.01M15 13h.01M9 21h6M7 21v-3M17 21v-3",
  backtest: "M3 12a9 9 0 1 0 3-6.7M3 4v5h5M12 8v4l3 2",
  brain: "M9 4a3 3 0 0 0-3 3a3 3 0 0 0-1 5.8V17a3 3 0 0 0 4 2.8M15 4a3 3 0 0 1 3 3a3 3 0 0 1 1 5.8V17a3 3 0 0 1-4 2.8M12 4v16",
  forge: "M12 3c3 3 5 6 5 9a5 5 0 0 1-10 0c0-3 2-6 5-9M12 12v9",
  ledger: "M5 3h13a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H5zM8 7h8M8 11h8M8 15h5",
  logbook: "M6 3h11a2 2 0 0 1 2 2v16H6zM6 3a2 2 0 0 0-2 2v14h2M10 8h5M10 12h5",
  verification: "M12 3l8 3v6c0 4-3.4 7.7-8 9c-4.6-1.3-8-5-8-9V6zM9 12l2 2l4-4",
  brokers: "M4 8h4v8H4zM11 5h4v14h-4zM18 10h3v6h-3M4 12h-2M18 12h-4",
  control: "M4 7h10M18 7h2M4 17h4M12 17h8M14 4v6M8 14v6",
  bell: "M6 9a6 6 0 1 1 12 0v5l2 3H4l2-3zM10 20a2 2 0 0 0 4 0",
  power: "M12 3v9M6.5 6.5a8 8 0 1 0 11 0",
  lock: "M6 11h12v9H6zM9 11V8a3 3 0 0 1 6 0v3M12 15v2",
  unlock: "M6 11h12v9H6zM9 11V8a3 3 0 0 1 5.6-1.5M12 15v2",
  play: "M8 5l11 7l-11 7z",
  pause: "M9 5v14M15 5v14",
  arrowUp: "M12 20V5M6 11l6-6l6 6",
  arrowDown: "M12 4v15M6 13l6 6l6-6",
  arrowRight: "M4 12h15M13 6l6 6l-6 6",
  chevronDown: "M6 9l6 6l6-6",
  chevronRight: "M9 6l6 6l-6 6",
  chevronLeft: "M15 6l-6 6l6 6",
  check: "M4 12l5 5L20 6",
  close: "M6 6l12 12M18 6L6 18",
  refresh: "M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5",
  plus: "M12 5v14M5 12h14",
  minus: "M5 12h14",
  external: "M14 4h6v6M20 4l-9 9M18 14v6H4V6h6",
  clock: "M12 3a9 9 0 1 0 0 18a9 9 0 0 0 0-18M12 7v5l3 2",
  database: "M12 3c4.4 0 8 1.3 8 3s-3.6 3-8 3s-8-1.3-8-3s3.6-3 8-3M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  activity: "M3 12h4l3-8l4 16l3-8h4",
  zap: "M13 2L4 14h6l-1 8l9-12h-6z",
  key: "M15 7a4 4 0 1 1-3.5 6L4 20H2v-2l7.5-7.5A4 4 0 0 1 15 7M16 8h.01",
  server: "M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01",
  wifi: "M2 8a16 16 0 0 1 20 0M5 12a11 11 0 0 1 14 0M8.5 15.5a5 5 0 0 1 7 0M12 19h.01",
  warning: "M12 3l9 17H3zM12 9v5M12 17h.01",
  info: "M12 3a9 9 0 1 0 0 18a9 9 0 0 0 0-18M12 11v6M12 7h.01",
  search: "M11 4a7 7 0 1 0 0 14a7 7 0 0 0 0-14M16 16l5 5",
  terminal: "M3 4h18v16H3zM7 9l3 3l-3 3M13 15h4",
  shield: "M12 3l8 3v6c0 4-3.4 7.7-8 9c-4.6-1.3-8-5-8-9V6z",
  download: "M12 4v11M7 11l5 5l5-5M4 20h16",
  filter: "M3 5h18l-7 8v6l-4 2v-8z",
  layer: "M12 3l9 5l-9 5l-9-5zM3 13l9 5l9-5",
  eye: "M2 12s4-7 10-7s10 7 10 7s-4 7-10 7S2 12 2 12M12 9a3 3 0 1 0 0 6a3 3 0 0 0 0-6",
  flame: "M12 3c3 4 5 7 5 10a5 5 0 0 1-10 0c0-2 1-4 2-5c0 2 1 3 3 3c0-3 0-5 0-8",
  pin: "M12 21s7-6.2 7-11a7 7 0 1 0-14 0c0 4.8 7 11 7 11M12 7a3 3 0 1 0 0 6a3 3 0 0 0 0-6",
};

interface IconProps {
  name: IconName;
  size?: number;
  className?: string;
  strokeWidth?: number;
  title?: string;
}

export function Icon({ name, size = 16, className, strokeWidth = 1.5, title }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden={title ? undefined : true}
      role={title ? "img" : undefined}
      focusable="false"
    >
      {title ? <title>{title}</title> : null}
      <path d={PATHS[name]} />
    </svg>
  );
}

export const ICON_NAMES = Object.keys(PATHS) as IconName[];
