import type { IconName } from "../components/Icon";

export interface Workstation {
  key: string;
  label: string;
  icon: IconName;
  group: "OPERATE" | "RESEARCH" | "GOVERN";
  blurb: string;
}

/** The 17 bespoke workstations, identical in both workspaces. */
export const WORKSTATIONS: Workstation[] = [
  { key: "dashboard", label: "Dashboard", icon: "grid", group: "OPERATE", blurb: "4-ring topology, NAV, silos, stream" },
  { key: "missions", label: "Missions", icon: "target", group: "OPERATE", blurb: "Multi-tranche saga orchestrator" },
  { key: "screener", label: "Screener", icon: "screener", group: "RESEARCH", blurb: "274 instruments, heatmaps, macro" },
  { key: "terminal", label: "Terminal", icon: "candles", group: "OPERATE", blurb: "Candles, studies, L2 ladder, ticket" },
  { key: "orders", label: "Orders", icon: "orders", group: "OPERATE", blurb: "Smart OMS, outbox, statutory costs" },
  { key: "options", label: "Options", icon: "options", group: "RESEARCH", blurb: "147 chains, GEX, payoff builder" },
  { key: "analyzer", label: "Analyzer", icon: "analyzer", group: "RESEARCH", blurb: "Shadow twin graduation nursery" },
  { key: "quant", label: "Quant", icon: "sigma", group: "RESEARCH", blurb: "490 factors, 47 loaders, pricer" },
  { key: "agents", label: "Agents", icon: "agents", group: "RESEARCH", blurb: "6 specialists, memory, grounded Q&A" },
  { key: "backtest", label: "Backtest", icon: "backtest", group: "RESEARCH", blurb: "Tax-net walk-forward, DSR gate" },
  { key: "brain", label: "Brain", icon: "brain", group: "GOVERN", blurb: "Ring supervisor, lifecycle, committee" },
  { key: "forge", label: "Forge", icon: "forge", group: "GOVERN", blurb: "Champion/challenger, drift, skills" },
  { key: "ledger", label: "Ledger", icon: "ledger", group: "GOVERN", blurb: "Double-entry IBOR, tax escrow" },
  { key: "logbook", label: "Logbook", icon: "logbook", group: "GOVERN", blurb: "Cost-drag attribution, R-multiples" },
  { key: "verification", label: "Verification", icon: "verification", group: "GOVERN", blurb: "TLA+ proof, VOPR, reconciler" },
  { key: "brokers", label: "Brokers", icon: "brokers", group: "GOVERN", blurb: "12 venues, egress, handshakes" },
  { key: "control", label: "Control", icon: "control", group: "GOVERN", blurb: "23 gates, alerts, settings, MCP" },
];

export const WORKSTATION_KEYS = WORKSTATIONS.map((station) => station.key);

export function workstation(key: string): Workstation | undefined {
  return WORKSTATIONS.find((station) => station.key === key);
}

export type Workspace = "paper" | "live";

export function workspacePath(workspace: Workspace, key: string): string {
  return `/${workspace}/${key}`;
}

export const GROUPS: Array<Workstation["group"]> = ["OPERATE", "RESEARCH", "GOVERN"];

export const RING_LEGEND = [
  { ring: "RING 0", label: "Sovereign Vault", detail: "signs, settles, never leaks keys" },
  { ring: "RING 1", label: "Spinal Reflex", detail: "<0.05ms deterministic hot path" },
  { ring: "RING 2", label: "Bicameral Brain", detail: "grounded reasoning, no broker socket" },
  { ring: "RING 3", label: "Glass Cockpit", detail: "operator control surface" },
] as const;

export const SILOS = [
  { id: "INDIA_ZERODHA_DHAN", short: "INDIA", floorBps: 8.5, currency: "INR" },
  { id: "CRYPTO_BINANCE", short: "CRYPTO", floorBps: 16.0, currency: "USDT" },
  { id: "US_IBKR", short: "US", floorBps: 2.8, currency: "USD" },
  { id: "FX_MT5", short: "FX", floorBps: 1.6, currency: "USD" },
] as const;

export const VAULTS = ["ReserveYield", "LongTermCompounder", "TacticalSwing", "SpinalScalper"] as const;
