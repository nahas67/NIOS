import { useEffect, useMemo, useState } from "react";

import { Heatmap, type HeatCell } from "../charts";
import { SILOS } from "../config/nav";
import {
  Badge,
  Banner,
  Button,
  Card,
  Cell,
  Checkbox,
  EmptyState,
  Field,
  KeyValue,
  PageHeader,
  Row,
  SelectInput,
  Spinner,
  Stat,
  StatGrid,
  Table,
  TextInput,
} from "../components/ui";
import { api, errorMessage } from "../lib/api";
import {
  asRecord,
  asRows,
  dateUtc,
  num,
  pct,
  priceMicros,
  safeBool,
  safeNumber,
  text,
  timeUtc,
} from "../lib/format";
import { rowsOf } from "../lib/payload";
import type { PageProps } from "./types";

interface Note {
  tone: "neutral" | "bull" | "bear";
  message: string;
}

const PAGE_SIZE = 25;
const ALL = "ALL";

function changeIndex(payload: unknown): Record<string, number> {
  const out: Record<string, number> = {};
  const record = asRecord(payload);
  for (const key of ["india_sectors", "us_gics_sectors", "crypto_narratives"]) {
    for (const sector of asRows(asRecord(record[key]).sectors)) {
      for (const member of asRows(sector.members)) {
        const symbol = text(member.symbol, "");
        if (symbol.length > 0) out[symbol] = safeNumber(member.change_pct, Number.NaN);
      }
    }
  }
  return out;
}

function sectorCells(payload: unknown): HeatCell[] {
  return asRows(asRecord(payload).sectors).map((row) => ({
    label: text(row.sector, "—"),
    value: safeNumber(row.avg_change_pct, 0),
    sub: `${safeNumber(row.member_count, 0)} names`,
  }));
}

function fxCells(payload: unknown): HeatCell[] {
  return asRows(asRecord(payload).currencies).map((row) => ({
    label: text(row.currency, "—"),
    value: safeNumber(row.relative_strength, 0),
    sub: num(row.policy_rate_pct, 2),
  }));
}

export default function ScreenerPage({ mode }: PageProps) {
  const [universe, setUniverse] = useState<Record<string, unknown>[]>([]);
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [serverTotal, setServerTotal] = useState(0);
  const [heatmaps, setHeatmaps] = useState<Record<string, unknown>>({});
  const [macro, setMacro] = useState<Record<string, unknown>>({});
  const [silo, setSilo] = useState(ALL);
  const [assetClass, setAssetClass] = useState(ALL);
  const [sector, setSector] = useState(ALL);
  const [search, setSearch] = useState("");
  const [optionsOnly, setOptionsOnly] = useState(false);
  const [page, setPage] = useState(0);
  const [reloadToken, setReloadToken] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<Note | null>(null);

  useEffect(() => {
    let active = true;
    const loadFacets = async () => {
      const [catalog, map, calendar] = await Promise.allSettled([
        api.get<unknown>("/instruments", { limit: 500, offset: 0 }),
        api.get<unknown>("/market/heatmaps"),
        api.get<unknown>("/market/macro-calendar"),
      ]);
      if (!active) return;
      if (catalog.status === "fulfilled") setUniverse(asRows(asRecord(catalog.value).items));
      else setError(errorMessage(catalog.reason));
      if (map.status === "fulfilled") setHeatmaps(asRecord(map.value));
      if (calendar.status === "fulfilled") setMacro(asRecord(calendar.value));
    };
    void loadFacets();
    return () => {
      active = false;
    };
  }, []);

  const filtersKey = `${silo}|${assetClass}|${sector}|${search}|${optionsOnly}`;

  useEffect(() => {
    let active = true;
    setLoading(true);
    const load = async () => {
      try {
        const payload = await api.get<unknown>("/instruments", {
          limit: 500,
          offset: 0,
          silo: silo === ALL ? undefined : silo,
          asset_class: assetClass === ALL ? undefined : assetClass,
          sector: sector === ALL ? undefined : sector,
          search: search.trim().length > 0 ? search.trim() : undefined,
          has_options: optionsOnly ? true : undefined,
        });
        if (!active) return;
        setRows(asRows(asRecord(payload).items));
        setServerTotal(safeNumber(asRecord(payload).total_count, 0));
        setError(null);
      } catch (caught) {
        if (!active) return;
        setRows([]);
        setServerTotal(0);
        setError(errorMessage(caught));
      } finally {
        if (active) setLoading(false);
      }
    };
    void load();
    return () => {
      active = false;
    };
  }, [filtersKey, silo, assetClass, sector, search, optionsOnly, reloadToken]);

  useEffect(() => {
    if (selected === null) {
      setDetail(null);
      return;
    }
    let active = true;
    setDetailLoading(true);
    const load = async () => {
      try {
        const payload = await api.get<unknown>(`/instruments/${encodeURIComponent(selected)}`);
        if (active) setDetail(asRecord(payload));
      } catch (caught) {
        if (!active) return;
        setDetail(null);
        setNote({ tone: "bear", message: `${selected}: ${errorMessage(caught)}` });
      } finally {
        if (active) setDetailLoading(false);
      }
    };
    void load();
    return () => {
      active = false;
    };
  }, [selected]);

  const classes = useMemo(() => {
    const tokens = new Set<string>();
    for (const row of universe) tokens.add(text(row.asset_class, ""));
    return [ALL, ...[...tokens].filter((token) => token.length > 0).sort()];
  }, [universe]);

  const sectors = useMemo(() => {
    const tokens = new Set<string>();
    for (const row of universe) tokens.add(text(row.sector, ""));
    return [ALL, ...[...tokens].filter((token) => token.length > 0).sort()];
  }, [universe]);

  const changes = useMemo(() => changeIndex(heatmaps), [heatmaps]);
  const filtered = useMemo(() => {
    const needle = search.trim().toUpperCase();
    return rows.filter((row) => {
      if (silo !== ALL && text(row.silo) !== silo) return false;
      if (assetClass !== ALL && text(row.asset_class) !== assetClass) return false;
      if (sector !== ALL && text(row.sector) !== sector) return false;
      if (optionsOnly && !safeBool(row.has_options, false)) return false;
      if (needle.length === 0) return true;
      return `${text(row.symbol)} ${text(row.display_name)}`.toUpperCase().includes(needle);
    });
  }, [rows, silo, assetClass, sector, optionsOnly, search]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(page, pageCount - 1);
  const visible = useMemo(
    () => filtered.slice(currentPage * PAGE_SIZE, currentPage * PAGE_SIZE + PAGE_SIZE),
    [filtered, currentPage],
  );

  const floorFor = (siloId: string) => SILOS.find((entry) => entry.id === siloId)?.floorBps ?? 0;
  const totalCount = Math.max(serverTotal, filtered.length);
  const macroRows = rowsOf(macro);
  const detailContract = asRecord(detail?.contract);
  const statutory = asRecord(detail?.statutory_cost);
  const surface = asRecord(detail?.options_surface);

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="SCREENER"
        subtitle="274-instrument universe · silo, class, sector filters · Finance Act 2026 cost floors"
        badges={
          <div className="flex items-center gap-1.5">
            <Badge tone={mode === "LIVE" ? "bear" : "accent"} mono>
              {mode}
            </Badge>
            <Badge tone="neutral" mono>
              {num(totalCount, 0)} INSTRUMENTS
            </Badge>
          </div>
        }
        actions={
          <Button
            size="sm"
            variant="subtle"
            icon="refresh"
            onClick={() => {
              setReloadToken((current) => current + 1);
              setNote({
                tone: "neutral",
                message: "Filters reapplied against the live contract registry.",
              });
            }}
          >
            Refresh
          </Button>
        }
      />

      {error !== null ? <Banner tone="bear">{error}</Banner> : null}
      {note !== null ? <Banner tone={note.tone}>{note.message}</Banner> : null}

      <StatGrid columns={4}>
        <Stat
          label="Universe"
          value={num(totalCount, 0)}
          hint={`${num(filtered.length, 0)} match the filter`}
        />
        <Stat
          label="Silos"
          value={num(SILOS.length, 0)}
          hint={SILOS.map((entry) => entry.short).join(" · ")}
        />
        <Stat
          label="Optionable"
          value={num(filtered.filter((row) => safeBool(row.has_options, false)).length, 0)}
          hint="has_options = true"
        />
        <Stat label="Page" value={`${currentPage + 1}/${pageCount}`} hint={`${PAGE_SIZE} rows per page`} />
      </StatGrid>

      <Card
        title="Instrument Catalogue"
        subtitle="contract specs, spot reference and statutory floor"
        icon="screener"
        dense
        actions={
          <Badge tone={loading ? "muted" : "bull"} mono>
            {loading ? "SYNC" : `${num(filtered.length, 0)} ROWS`}
          </Badge>
        }
      >
        <div className="grid gap-2 border-b border-hairline p-2 md:grid-cols-2 xl:grid-cols-5">
          <Field label="Silo">
            <SelectInput
              value={silo}
              onChange={setSilo}
              options={[ALL, ...SILOS.map((entry) => entry.id)].map((value) => ({
                value,
                label: value === ALL ? "ALL SILOS" : value,
              }))}
            />
          </Field>
          <Field label="Asset class">
            <SelectInput
              value={assetClass}
              onChange={setAssetClass}
              options={classes.map((value) => ({ value, label: value === ALL ? "ALL CLASSES" : value }))}
            />
          </Field>
          <Field label="Sector">
            <SelectInput
              value={sector}
              onChange={setSector}
              options={sectors.map((value) => ({ value, label: value === ALL ? "ALL SECTORS" : value }))}
            />
          </Field>
          <Field label="Search">
            <TextInput
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="symbol, name, sector"
            />
          </Field>
          <div className="flex items-end pb-1">
            <Checkbox checked={optionsOnly} onChange={setOptionsOnly} label="Optionable only" />
          </div>
        </div>

        {loading && visible.length === 0 ? (
          <div className="flex items-center gap-2 p-3 text-[11px] text-ink-3">
            <Spinner /> loading the contract registry
          </div>
        ) : visible.length === 0 ? (
          <div className="p-3">
            <EmptyState
              title="No instruments"
              hint="Relax a filter — the registry query returned zero contracts."
            />
          </div>
        ) : (
          <Table columns={["SYMBOL", "NAME", "SILO", "CLASS", "SECTOR", "LAST", "CHG%", "TAX FLOOR", "OPT"]}>
            {visible.map((row) => {
              const symbol = text(row.symbol, "—");
              const siloId = text(row.silo, "—");
              const summary = asRecord(row.feature_summary);
              const last = safeNumber(summary.spot_reference, safeNumber(row.spot_reference, Number.NaN));
              const change = safeNumber(changes[symbol], Number.NaN);
              const active = symbol === selected;
              return (
                <Row key={symbol} onClick={() => setSelected(symbol)} tone={active ? "bull" : undefined}>
                  <Cell mono className="text-ink">
                    {symbol}
                  </Cell>
                  <Cell className="max-w-[220px] truncate">{text(row.display_name)}</Cell>
                  <Cell mono className="text-ink-3">
                    {siloId}
                  </Cell>
                  <Cell className="text-ink-2">{text(row.asset_class)}</Cell>
                  <Cell className="max-w-[150px] truncate text-ink-2">{text(row.sector)}</Cell>
                  <Cell align="right">{Number.isFinite(last) ? num(last, 2) : "—"}</Cell>
                  <Cell
                    align="right"
                    tone={Number.isFinite(change) ? (change >= 0 ? "bull" : "bear") : "muted"}
                  >
                    {Number.isFinite(change) ? pct(change, 2) : "—"}
                  </Cell>
                  <Cell align="right" className="text-ink-2">
                    {num(floorFor(siloId), 2)} bps
                  </Cell>
                  <Cell align="right">
                    <Badge tone={safeBool(row.has_options, false) ? "accent" : "muted"} mono>
                      {safeBool(row.has_options, false) ? "OPT" : "—"}
                    </Badge>
                  </Cell>
                </Row>
              );
            })}
          </Table>
        )}

        <div className="flex items-center justify-between gap-2 border-t border-hairline px-2 py-1.5">
          <span className="text-[10px] uppercase tracking-[0.08em] text-ink-3">
            rows {filtered.length === 0 ? 0 : currentPage * PAGE_SIZE + 1}–
            {Math.min(filtered.length, (currentPage + 1) * PAGE_SIZE)} of {filtered.length}
          </span>
          <div className="flex items-center gap-1.5">
            <Button
              size="sm"
              variant="ghost"
              disabled={currentPage === 0}
              onClick={() => setPage(Math.max(0, currentPage - 1))}
            >
              Prev
            </Button>
            <Badge tone="neutral" mono>
              {currentPage + 1}/{pageCount}
            </Badge>
            <Button
              size="sm"
              variant="ghost"
              disabled={currentPage >= pageCount - 1}
              onClick={() => setPage(Math.min(pageCount - 1, currentPage + 1))}
            >
              Next
            </Button>
          </div>
        </div>
      </Card>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="flex min-w-0 flex-col gap-3">
          <div className="grid gap-3 md:grid-cols-2">
            <Card title="India Sectors" subtitle="avg 1d change" icon="grid">
              <Heatmap cells={sectorCells(heatmaps.india_sectors)} columns={4} cellHeight={42} />
            </Card>
            <Card title="US GICS Sectors" subtitle="avg 1d change" icon="grid">
              <Heatmap cells={sectorCells(heatmaps.us_gics_sectors)} columns={4} cellHeight={42} />
            </Card>
            <Card title="Crypto Narratives" subtitle="avg 1d change" icon="grid">
              <Heatmap cells={sectorCells(heatmaps.crypto_narratives)} columns={4} cellHeight={42} />
            </Card>
            <Card title="FX G10 Strength" subtitle="relative strength" icon="grid">
              <Heatmap cells={fxCells(heatmaps.fx_g10_strength)} columns={4} cellHeight={42} />
            </Card>
          </div>

          <Card
            title="Macro Calendar"
            subtitle="release schedule with volatility multiplier"
            icon="clock"
            dense
            actions={
              <Badge tone="neutral" mono>
                {macroRows.length}
              </Badge>
            }
          >
            {macroRows.length === 0 ? (
              <div className="p-3">
                <EmptyState
                  title="No releases"
                  hint="The macro calendar payload carried no events for this window."
                />
              </div>
            ) : (
              <Table columns={["WHEN", "REGION", "EVENT", "IMPACT", "VOL X"]} dense>
                {macroRows.slice(0, 12).map((event, index) => {
                  const tier = text(event.impact_tier, "MEDIUM");
                  return (
                    <Row key={`${text(event.event_id, "event")}-${index}`}>
                      <Cell mono>{`${dateUtc(event.release_utc)} ${timeUtc(event.release_utc)}`}</Cell>
                      <Cell mono>{text(event.region, "—")}</Cell>
                      <Cell className="max-w-[280px] truncate">{text(event.title)}</Cell>
                      <Cell align="right">
                        <Badge tone={tier === "HIGH" ? "bear" : "accent"} mono>
                          {tier}
                        </Badge>
                      </Cell>
                      <Cell align="right" tone="muted">
                        {num(event.volatility_multiplier, 2)}
                      </Cell>
                    </Row>
                  );
                })}
              </Table>
            )}
          </Card>
        </div>

        <Card
          title="Instrument Detail"
          subtitle={selected === null ? "select a row" : selected}
          icon="search"
          actions={
            selected === null ? null : (
              <Button size="sm" variant="ghost" onClick={() => setSelected(null)}>
                Close
              </Button>
            )
          }
        >
          {selected === null ? (
            <EmptyState
              title="No instrument selected"
              hint="Click a catalogue row to open the contract, tax and options detail."
            />
          ) : detailLoading ? (
            <div className="flex items-center gap-2 text-[11px] text-ink-3">
              <Spinner /> loading contract
            </div>
          ) : detail === null ? (
            <EmptyState
              title="Detail unavailable"
              hint="The gateway refused or could not resolve this instrument."
            />
          ) : (
            <div className="flex flex-col gap-2">
              <KeyValue
                columns={1}
                items={[
                  { label: "Symbol", value: text(detailContract.symbol, selected) },
                  {
                    label: "Venue",
                    value: `${text(detailContract.venue, "—")} · ${text(detailContract.currency, "—")}`,
                  },
                  {
                    label: "Asset class",
                    value: `${text(detailContract.asset_class, "—")} · ${text(detailContract.sub_category, "—")}`,
                  },
                  { label: "Tick size", value: `${priceMicros(detailContract.tick_size_micros, 4)}` },
                  { label: "Lot size", value: num(detailContract.lot_size, 4) },
                  { label: "Freeze qty", value: num(detailContract.freeze_quantity, 0) },
                  {
                    label: "Circuit band",
                    value: `${num(asRecord(detailContract.circuit_band_pct).lower, 2)}% / ${num(asRecord(detailContract.circuit_band_pct).upper, 2)}%`,
                  },
                  { label: "Margin initial", value: pct(detailContract.margin_initial_pct, 2) },
                  { label: "Settlement", value: text(detailContract.settlement_cycle, "—") },
                ]}
              />

              <div className="border-t border-hairline pt-2">
                <div className="mb-1 text-[10px] font-medium uppercase tracking-[0.14em] text-ink-3">
                  Finance Act 2026 · segment legs
                </div>
                {asRows(statutory.components).length === 0 ? (
                  <div className="text-[11px] text-ink-3">No statutory breakdown in this payload.</div>
                ) : (
                  <div className="flex flex-col gap-1">
                    {asRows(statutory.components).map((line, index) => (
                      <div
                        key={`${text(line.key, "line")}-${index}`}
                        className="flex items-baseline justify-between gap-2 border-b border-hairline/60 pb-0.5 text-ink-2"
                      >
                        <span className="truncate text-[10.5px]">{text(line.label)}</span>
                        <span className="shrink-0 text-[10.5px] tabular-nums">
                          {num(line.bps, 3)} bps · {num(line.micros, 0)} µ
                        </span>
                      </div>
                    ))}
                    <div className="flex items-baseline justify-between gap-2 pt-1 text-[11px]">
                      <span className="uppercase tracking-[0.1em] text-ink-3">Total floor</span>
                      <span className="tabular-nums text-accent">{num(statutory.total_bps, 2)} bps</span>
                    </div>
                  </div>
                )}
              </div>

              {safeBool(detailContract.has_options, false) ? (
                <div className="border-t border-hairline pt-2">
                  <div className="mb-1 text-[10px] font-medium uppercase tracking-[0.14em] text-ink-3">
                    Options surface
                  </div>
                  <KeyValue
                    columns={2}
                    items={[
                      { label: "IV rank", value: num(surface.iv_rank, 2) },
                      { label: "Net GEX", value: num(surface.net_gamma_exposure_usd, 0) },
                      { label: "Strikes", value: num(surface.strike_count, 0) },
                      { label: "Spot ref", value: num(surface.spot_reference, 2) },
                    ]}
                  />
                </div>
              ) : null}
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

export { ScreenerPage };
