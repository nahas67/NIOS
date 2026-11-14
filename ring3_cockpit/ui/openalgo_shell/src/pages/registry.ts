/**
 * The fixed V1.4 page catalog.
 *
 * Fifteen production pages, five operational domains, one canonical route per
 * page plus the aliases the backend advertises in `/api/v2/cockpit/pages`.
 * This module is metadata only: it never imports a page component, so the
 * sidebar, the command palette and the router can share it without a cycle.
 */

export type PageKey =
  | "dashboard"
  | "terminal"
  | "orders"
  | "options"
  | "analyzer"
  | "quant"
  | "agents"
  | "backtest"
  | "brain"
  | "forge"
  | "ledger"
  | "logbook"
  | "verification"
  | "brokers"
  | "control";

export type GroupId = "core" | "quant" | "brain" | "audit" | "sovereign";

export interface PageMeta {
  key: PageKey;
  title: string;
  group: GroupId;
  route: string;
  aliases: string[];
  ring: string;
  summary: string;
  /** Short label used by the collapsed rail and the palette. */
  short: string;
}

export interface NavGroup {
  id: GroupId;
  roman: string;
  label: string;
}

export const NAV_GROUPS: NavGroup[] = [
  { id: "core", roman: "I", label: "Core Execution" },
  { id: "quant", roman: "II", label: "Quant & Research" },
  { id: "brain", roman: "III", label: "Bicameral Brain" },
  { id: "audit", roman: "IV", label: "Ledger & Audit" },
  { id: "sovereign", roman: "V", label: "Sovereign Control" },
];

export const PAGES: PageMeta[] = [
  {
    key: "dashboard",
    title: "Sovereign Command Dashboard",
    short: "Dashboard",
    group: "core",
    route: "/dashboard",
    aliases: ["/"],
    ring: "ring3",
    summary:
      "Four-ring isolation heartbeat, total IBOR NAV, the 4×4 silo vault grid and the grounded proof stream.",
  },
  {
    key: "terminal",
    title: "Institutional Market Terminal",
    short: "Market Terminal",
    group: "core",
    route: "/terminal",
    aliases: [],
    ring: "ring1",
    summary:
      "Candles with MA5/10/20 and VWAP, a Lee-Ready time & sales tape and the 10-level L2 depth ladder.",
  },
  {
    key: "orders",
    title: "SmartOMS Orders & Positions",
    short: "Orders & Positions",
    group: "core",
    route: "/orders",
    aliases: ["/positions"],
    ring: "ring0",
    summary:
      "Maker-Peg and TWAP/Iceberg working orders, the crash-atomic outbox and per-silo reconciled positions.",
  },
  {
    key: "options",
    title: "Options Chain & Strategy Lab",
    short: "Options Chain",
    group: "core",
    route: "/options",
    aliases: ["/chain"],
    ring: "ring2",
    summary:
      "Symmetric 11-strike chain with Greeks, PCR and Max Pain beside a grounded multi-leg payoff builder.",
  },
  {
    key: "analyzer",
    title: "Shadow Twin & 30-Fill Nursery",
    short: "Shadow Analyzer",
    group: "quant",
    route: "/analyzer",
    aliases: [],
    ring: "ring2",
    summary:
      "L2 shadow execution twin, adverse-selection Q0.98 comparison and the graduation ladder to live.",
  },
  {
    key: "quant",
    title: "490-Factor Quant Lab",
    short: "490-Factor Quant Lab",
    group: "quant",
    route: "/quant",
    aliases: [],
    ring: "ring2",
    summary:
      "Ranked factor matrix, 47 loaders × 17 regional engines readiness and the grounded Black-Scholes pricer.",
  },
  {
    key: "agents",
    title: "Agent Task Hub",
    short: "Agent Task Hub",
    group: "quant",
    route: "/agents",
    aliases: [],
    ring: "ring2",
    summary:
      "Six specialist agents, the token budget, grounded task assignment and the 0..100 decision dashboard.",
  },
  {
    key: "backtest",
    title: "Walk-Forward Backtester",
    short: "Walk-Forward Backtester",
    group: "quant",
    route: "/backtest",
    aliases: [],
    ring: "ring2",
    summary:
      "Tax-net walk-forward runs with Deflated Sharpe, purged K-fold certification and horizon accuracy.",
  },
  {
    key: "brain",
    title: "Brain & Contagion X-Ray",
    short: "Brain & Contagion X-Ray",
    group: "brain",
    route: "/brain",
    aliases: ["/xray"],
    ring: "ring2",
    summary:
      "Ring process isolation matrix, the MiroFish five-archetype shock cascade and the committee transcript.",
  },
  {
    key: "forge",
    title: "Alpha Forge & Skills",
    short: "Alpha Forge & Skills",
    group: "brain",
    route: "/forge",
    aliases: [],
    ring: "ring2",
    summary:
      "Champion/challenger leaderboard, DSR promotion gates and the progressive-disclosure SKILL.md inspector.",
  },
  {
    key: "ledger",
    title: "IBOR Double-Entry Ledger",
    short: "IBOR Ledger",
    group: "audit",
    route: "/ledger",
    aliases: [],
    ring: "ring0",
    summary:
      "Debit/credit conservation, the 4 silos × 4 vaults micro-unit grid, ratchet preview and tax floors.",
  },
  {
    key: "logbook",
    title: "Structured Trade Logbook",
    short: "Trade Logbook",
    group: "audit",
    route: "/logbook",
    aliases: ["/journal"],
    ring: "ring3",
    summary:
      "Gross → cost drag → net P&L attribution, R-multiples and the chronological execution journal.",
  },
  {
    key: "verification",
    title: "Formal Proofs & Chaos",
    short: "Formal Proofs & Chaos",
    group: "audit",
    route: "/verification",
    aliases: [],
    ring: "ring0",
    summary:
      "TLA+ state-space proof, seeded VOPR faults, SIGKILL recovery drills and OS/socket tuning audit.",
  },
  {
    key: "brokers",
    title: "12-Venue Broker Hub",
    short: "12-Venue Broker Hub",
    group: "sovereign",
    route: "/brokers",
    aliases: ["/venues"],
    ring: "ring0",
    summary:
      "Four silos × three venues, write-only credential masks, dual-vendor feeds and Ed25519 signing pings.",
  },
  {
    key: "control",
    title: "Control Room & MCP",
    short: "Control Room & MCP",
    group: "sovereign",
    route: "/control",
    aliases: ["/mcp"],
    ring: "ring3",
    summary:
      "22-gate go-live certificate, credential vault, key rotation, LLM pool probe and the air-gapped MCP console.",
  },
];

export const PAGE_KEYS: PageKey[] = PAGES.map((page) => page.key);

export const PAGE_BY_KEY: Record<PageKey, PageMeta> = PAGES.reduce(
  (acc, page) => {
    acc[page.key] = page;
    return acc;
  },
  {} as Record<PageKey, PageMeta>,
);

const ALIAS_INDEX: Record<string, PageKey> = PAGES.reduce(
  (acc, page) => {
    acc[page.route.replace(/^\//, "")] = page.key;
    for (const alias of page.aliases) {
      const clean = alias.replace(/^\//, "");
      if (clean !== "") acc[clean] = page.key;
    }
    return acc;
  },
  {} as Record<string, PageKey>,
);

export function isPageKey(value: string): value is PageKey {
  return Object.hasOwn(PAGE_BY_KEY, value);
}

/** Resolve a path segment (`positions`, `xray`, `dashboard`) to a page key. */
export function pageFromSegment(segment: string): PageKey | null {
  const clean = segment.trim().toLowerCase().replace(/\/+$/, "");
  if (clean === "") return "dashboard";
  if (isPageKey(clean)) return clean;
  return ALIAS_INDEX[clean] ?? null;
}

export function pagesByGroup(group: GroupId): PageMeta[] {
  return PAGES.filter((page) => page.group === group);
}
