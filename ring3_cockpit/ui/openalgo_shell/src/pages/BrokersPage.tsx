/**
 * Twelve-venue broker hub.
 *
 * Four silos by three venues. Credentials are write-only and encrypted at rest;
 * the cockpit can prove a credential exists and can ping the venue, but it can
 * never read the secret back.
 */

import { useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { num, rows, str } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { toast } from "@/lib/toast";
import {
  Btn,
  DataGrid,
  EmptyCleanSlate,
  Field,
  MetricTile,
  SectionHeader,
  StatusBadge,
  TerminalPanel,
} from "@/design-system/primitives";

export function BrokersPage(): ReactNode {
  const page = useApi(() => api.brokers(), []);
  const [ping, setPing] = useState<Record<string, unknown> | null>(null);
  const [silo, setSilo] = useState("INDIA");
  const [venue, setVenue] = useState("DHAN");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [busy, setBusy] = useState(false);

  const venues = rows(page.data, "venues");
  const silos = rows(page.data, "silos");
  const signing = page.data?.signing_dry_run;
  const feed = page.data?.feed;
  const airGap = page.data?.air_gap;
  const supported = num(page.data, "venues_supported", venues.length || 12);

  const doPing = async (target: string) => {
    setBusy(true);
    try {
      const next = await api.brokerPing(target);
      setPing(next);
      toast(`${target} signing ping returned`, "success");
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "ping rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  const store = async () => {
    setBusy(true);
    try {
      await api.brokerCredentials({ silo, venue, api_key: apiKey, api_secret: apiSecret });
      toast(`${venue} credential sealed for ${silo}`, "success");
      setApiKey("");
      setApiSecret("");
      page.reload();
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "credential rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="nx-page">
      <SectionHeader
        kicker="RING 0 - VENUES"
        title="12-Venue Broker Hub"
        sub="Three venues per silo, each behind an encrypted adapter. Ring 2 never holds one of these keys and never opens an exchange socket."
        actions={
          <>
            <StatusBadge tone="bull" dot>
              {supported} VENUES DECLARED
            </StatusBadge>
            <Btn size="sm" tone="ghost" onClick={page.reload}>
              Refresh hub
            </Btn>
          </>
        }
      />

      <div className="nx-grid nx-grid--4">
        <MetricTile label="Venues" value={String(supported)} sub="across four silos" />
        <MetricTile label="Silos wired" value={String(silos.length || 4)} sub="capital never crosses" />
        <MetricTile
          label="Signing dry run"
          value={str(signing, "status", "UNSIGNED").toUpperCase()}
          sub="Ed25519 keystore"
          tone={str(signing, "status", "").toUpperCase() === "PASS" ? "bull" : "neutral"}
          badge={<StatusBadge tone="muted">NO LIVE ORDER</StatusBadge>}
        />
        <MetricTile
          label="Feed posture"
          value={str(feed, "mode", "DUAL").toUpperCase()}
          sub={`lag ${num(feed, "lag_ms", 0)} ms, divergence ${num(feed, "divergence_pct", 0).toFixed(2)}%`}
        />
      </div>

      <div className="nx-grid nx-grid--3-2">
        <TerminalPanel title="Venue matrix" subtitle="Ping proves the adapter, not the liquidity" flush>
          <DataGrid
            columns={[
              {
                key: "venue",
                label: "Venue",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-strong">{str(row, "venue", str(row, "name", "-"))}</span>
                ),
              },
              {
                key: "silo",
                label: "Silo",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-dim nx-xs">{str(row, "silo", "-")}</span>
                ),
              },
              {
                key: "credential",
                label: "Credential",
                render: (row: Record<string, unknown>) => {
                  const stored = Boolean(row.credential_present) || str(row, "credential", "") === "STORED";
                  return <StatusBadge tone={stored ? "bull" : "muted"}>{stored ? "SEALED" : "MISSING"}</StatusBadge>;
                },
              },
              {
                key: "status",
                label: "Ping",
                align: "right",
                render: (row: Record<string, unknown>) => (
                  <Btn
                    size="sm"
                    tone="ghost"
                    onClick={() => void doPing(str(row, "venue", str(row, "name", "DHAN")))}
                    disabled={busy}
                  >
                    Ping
                  </Btn>
                ),
              },
            ]}
            rows={venues}
            getKey={(row, index) => `${str(row, "venue", "v")}-${index}`}
            empty={
              <EmptyCleanSlate
                title="No venue adapter reported"
                body="The hub starts with no credential sealed. Store one below to arm an adapter in sandbox mode."
                hint="POST /api/v2/control/brokers/credentials"
              />
            }
          />
        </TerminalPanel>

        <div className="nx-stack">
          <TerminalPanel title="Seal a credential" subtitle="Write-only, Fernet + Argon2 at rest">
            <Field label="Silo">
              <select className="nx-select" value={silo} onChange={(event) => setSilo(event.target.value)}>
                <option value="INDIA">INDIA</option>
                <option value="US">US</option>
                <option value="FX">FX</option>
                <option value="CRYPTO">CRYPTO</option>
              </select>
            </Field>
            <Field label="Venue">
              <select className="nx-select" value={venue} onChange={(event) => setVenue(event.target.value)}>
                {(venues.length > 0
                  ? venues.map((row) => str(row, "venue", str(row, "name", "VENUE")))
                  : ["DHAN", "ZERODHA", "ANGEL", "UPSTOX", "IBKR", "MT5", "BINANCE", "OKX", "BYBIT"]
                ).map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="API key">
              <input
                className="nx-input"
                type="password"
                autoComplete="off"
                value={apiKey}
                onChange={(event) => setApiKey(event.target.value)}
              />
            </Field>
            <Field label="API secret">
              <input
                className="nx-input"
                type="password"
                autoComplete="off"
                value={apiSecret}
                onChange={(event) => setApiSecret(event.target.value)}
              />
            </Field>
            <Btn tone="primary" block onClick={store} disabled={busy} style={{ marginTop: 8 }}>
              Seal credential
            </Btn>
            <p className="nx-xs nx-muted" style={{ marginTop: 6 }}>
              Secrets are never echoed back. A failed ping proves the adapter, never the market.
            </p>
          </TerminalPanel>

          <TerminalPanel title="Last ping" subtitle="Ed25519 signing dry run">
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Venue</span>
              <span className="nx-sm nx-strong">{str(ping, "venue", "-")}</span>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Latency</span>
              <span className="nx-sm nx-strong">{num(ping, "latency_ms", 0).toFixed(2)} ms</span>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Signature</span>
              <StatusBadge tone={str(ping, "signature", "") ? "bull" : "muted"}>
                {str(ping, "signature", "UNSIGNED").toUpperCase().slice(0, 16)}
              </StatusBadge>
            </div>
            <div className="nx-hr" />
            <span className="nx-xs nx-muted">
              {str(airGap, "status", "Ring 2 holds zero broker keys and opens zero exchange sockets.")}
            </span>
          </TerminalPanel>
        </div>
      </div>
    </div>
  );
}
