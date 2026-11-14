/**
 * SmartOMS orders, positions and the atomic outbox.
 *
 * Working Maker-Peg and TWAP/Iceberg slices on the left, the crash-atomic
 * outbox and reconciled positions on the right. Cancellation is the only
 * mutating action exposed here; new exposure is proposed by Ring 1 or Ring 2,
 * never typed into a cockpit form.
 */

import { useState, type ReactNode } from "react";
import { api, postCancelOrders } from "@/lib/api";
import { num, rows, str, titleCase } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { toast } from "@/lib/toast";
import {
  Btn,
  ConfirmModal,
  DataGrid,
  EmptyCleanSlate,
  MetricTile,
  SectionHeader,
  StatusBadge,
  TerminalPanel,
} from "@/design-system/primitives";

export function OrdersPage(): ReactNode {
  const page = useApi(() => api.orders(), []);
  const [cancelOpen, setCancelOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const working = rows(page.data, "working_orders");
  const twaps = rows(page.data, "twap_icebergs");
  const reservations = rows(page.data, "reservations");
  const events = rows(page.data, "events");
  const pending = num(page.data, "pending_reservations", 0);
  const positions = page.data?.positions;
  const positionRows = Array.isArray(positions)
    ? positions.filter((row): row is Record<string, unknown> => typeof row === "object" && row !== null)
    : Object.entries(typeof positions === "object" && positions !== null ? positions : {}).map(
        ([silo, value]) => ({ silo, ...(typeof value === "object" && value !== null ? value : {}) }),
      );

  const cancelAll = async () => {
    setBusy(true);
    try {
      const receipt = await postCancelOrders("ALL");
      toast(`Cancelled ${String(receipt.cancelled ?? "all")} working orders`, "success");
      setCancelOpen(false);
      page.reload();
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "cancel rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="nx-page">
      <SectionHeader
        kicker="RING 0 - SMART OMS"
        title="Orders, slices and reconciled positions"
        sub="Maker-Peg re-pegs at most twice on a 3-second clock; TWAP and Iceberg never exceed 10% of the top of book."
        actions={
          <>
            <StatusBadge tone={pending > 0 ? "accent" : "muted"}>
              {pending} PENDING OUTBOX
            </StatusBadge>
            <Btn size="sm" tone="ghost" onClick={page.reload}>
              Refresh blotter
            </Btn>
            <Btn size="sm" tone="danger" onClick={() => setCancelOpen(true)} disabled={working.length === 0}>
              Cancel all working
            </Btn>
          </>
        }
      />

      <div className="nx-grid nx-grid--4">
        <MetricTile label="Working orders" value={String(working.length)} sub="Maker-Peg chasers" />
        <MetricTile label="TWAP / Iceberg" value={String(twaps.length)} sub="Sliced child orders" />
        <MetricTile
          label="Outbox reservations"
          value={String(reservations.length)}
          sub="TigerBeetle two-phase"
          badge={<StatusBadge tone={pending > 0 ? "accent" : "bull"}>{pending > 0 ? "STAGED" : "CLEAN"}</StatusBadge>}
        />
        <MetricTile
          label="Reconciliation"
          value={str(page.data, "reconciliation.available", "false") === "true" ? "TRACKED" : String(num(page.data, "reconciliation.tracked_positions", 0))}
          sub="60s drift check, freeze above 0.01%"
        />
      </div>

      <div className="nx-grid nx-grid--2-1">
        <TerminalPanel
          title="Working blotter"
          subtitle="Resting liquidity is never treated as a fill"
          flush
        >
          <DataGrid
            columns={[
              {
                key: "id",
                label: "Order",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-mono">{str(row, "order_id", str(row, "slicer_id", "-")).slice(0, 12)}</span>
                ),
              },
              {
                key: "kind",
                label: "Kind",
                render: (row: Record<string, unknown>) => (
                  <StatusBadge tone="accent">{str(row, "kind", "MAKER_PEG")}</StatusBadge>
                ),
              },
              {
                key: "side",
                label: "Side",
                render: (row: Record<string, unknown>) => {
                  const side = str(row, "side", "-").toUpperCase();
                  return <StatusBadge tone={side.startsWith("B") ? "bull" : "bear"}>{side}</StatusBadge>;
                },
              },
              {
                key: "price",
                label: "Peg price",
                align: "right",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-num">{num(row, "price", num(row, "limit_price", 0)).toFixed(2)}</span>
                ),
              },
              {
                key: "repegs",
                label: "Re-pegs left",
                align: "right",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-num nx-dim">{num(row, "repegs_remaining", 0)}</span>
                ),
              },
              {
                key: "status",
                label: "Status",
                align: "right",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-muted nx-xs">{titleCase(str(row, "status", "WORKING"))}</span>
                ),
              },
            ]}
            rows={working}
            getKey={(row, index) => `${str(row, "order_id", "order")}-${index}`}
            empty={
              <EmptyCleanSlate
                title="No working order"
                body="A clean slate holds zero resting orders. Ring 1 may propose a scalp only after the taker OFI gate clears."
                hint="TakerBuyRatio_5s >= 0.65"
              />
            }
          />
        </TerminalPanel>

        <div className="nx-stack">
          <TerminalPanel title="Atomic outbox" subtitle="Two-phase pending transfers" flush>
            <DataGrid
              columns={[
                {
                  key: "id",
                  label: "Reservation",
                  render: (row: Record<string, unknown>) => (
                    <span className="nx-mono">{str(row, "id", str(row, "reservation_id", "-")).slice(0, 10)}</span>
                  ),
                },
                {
                  key: "amount",
                  label: "Micros",
                  align: "right",
                  render: (row: Record<string, unknown>) => (
                    <span className="nx-num">{num(row, "amount_micros", 0).toLocaleString("en-US")}</span>
                  ),
                },
              ]}
              rows={reservations}
              empty={<EmptyCleanSlate title="Outbox clean" body="No pending transfer is staged against the ledger." />}
            />
          </TerminalPanel>

          <TerminalPanel title="Reconciled positions" subtitle="Broker versus IBOR, per silo">
            {positionRows.length === 0 ? (
              <EmptyCleanSlate title="Flat everywhere" body="No position is carried in any silo." />
            ) : (
              <div className="nx-list nx-scroll nx-scroll--sm">
                {positionRows.slice(0, 10).map((row, index) => (
                  <div className="nx-list-item" key={String(index)}>
                    <span className="nx-strong nx-sm">{str(row, "symbol", str(row, "silo", "POSITION"))}</span>
                    <span className="nx-xs nx-muted">{str(row, "silo", "")}</span>
                    <span className="nx-num" style={{ marginLeft: "auto" }}>
                      {num(row, "quantity", num(row, "qty", 0))}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </TerminalPanel>
        </div>
      </div>

      <TerminalPanel title="OMS event log" subtitle="Last 40 transitions" flush>
        <DataGrid
          columns={[
            {
              key: "ts",
              label: "Time",
              render: (row: Record<string, unknown>) => (
                <span className="nx-mono nx-muted">{str(row, "ts", str(row, "at", "-")).slice(0, 19)}</span>
              ),
            },
            {
              key: "type",
              label: "Event",
              render: (row: Record<string, unknown>) => (
                <StatusBadge tone="muted">{str(row, "type", str(row, "event", "-"))}</StatusBadge>
              ),
            },
            {
              key: "detail",
              label: "Detail",
              render: (row: Record<string, unknown>) => (
                <span className="nx-xs nx-dim nx-truncate">{str(row, "detail", str(row, "reason", "-"))}</span>
              ),
            },
          ]}
          rows={events}
          getKey={(row, index) => `${str(row, "ts", "e")}-${index}`}
          empty={<EmptyCleanSlate title="No OMS transition yet" body="The event ring is empty until the first proposal is staged." />}
        />
      </TerminalPanel>

      <ConfirmModal
        open={cancelOpen}
        title="Cancel every working order"
        description="This issues a cancel-all against the Smart OMS. It cannot be undone; a new proposal is required to re-enter."
        confirmLabel="Cancel all"
        tone="danger"
        busy={busy}
        onConfirm={cancelAll}
        onClose={() => setCancelOpen(false)}
      >
        <p className="nx-portal-note">{working.length} working order(s) will be pulled from the book.</p>
      </ConfirmModal>
    </div>
  );
}
