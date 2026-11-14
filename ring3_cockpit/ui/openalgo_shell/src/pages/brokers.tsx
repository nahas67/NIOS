import { useState } from 'react'
import {
  CheckCircle2,
  Fingerprint,
  KeyRound,
  Radio,
  ServerCog,
  ShieldCheck,
  XCircle,
} from 'lucide-react'

import { ActionFeedback, ProofRow } from '@/components/operator-action'
import { DataState, PageShell, StatTile } from '@/components/page-shell'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useAction } from '@/hooks/use-action'
import { usePageData } from '@/hooks/use-page-data'
import { fetchBrokersPage, postBrokerPing } from '@/lib/api'
import { truncateDigest } from '@/lib/format'
import type { BrokerPingReceipt } from '@/types'

const SILO_ORDER = ['CRYPTO_BINANCE', 'INDIA_ZERODHA_DHAN', 'US_IBKR', 'FX_MT5'] as const

export function BrokersPage() {
  const { data, error, loading, reload } = usePageData(fetchBrokersPage)
  const ping = useAction<BrokerPingReceipt>()
  const [venue, setVenue] = useState<string | null>(null)

  const venues = data?.venues ?? []
  const counts = data?.counts
  const vault = data?.vault

  async function probe(target: string) {
    setVenue(target)
    await ping.run(() => postBrokerPing(target))
  }

  return (
    <PageShell
      title="12-Venue Broker Hub & Feed Multiplexer"
      ring="RING 0"
      capability="READ-ONLY"
      subtitle="Every adapter across all four sovereign silos: credential state (never a raw key), dry-run Ed25519 signing, dual-vendor feed health and an interactive signing probe. Ring 3 renders the matrix; it never holds a venue secret."
      actions={
        <Badge variant="outline" className="font-mono text-[10px]">
          {counts?.venues ?? 0} VENUES · {counts?.silos ?? 0} SILOS
        </Badge>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          icon={ServerCog}
          label="Venues"
          value={`${counts?.venues ?? 0}`}
          hint={`${counts?.adapters_importable ?? 0} adapters importable`}
        />
        <StatTile
          icon={KeyRound}
          label="Configured"
          value={`${counts?.configured ?? 0}`}
          hint={`${counts?.unset ?? 0} unset · write-only vault`}
        />
        <StatTile
          icon={Fingerprint}
          label="Ed25519 fingerprint"
          value={vault?.ed25519_public_fingerprint ?? '—'}
          hint={`key version ${vault?.key_version ?? '—'}`}
        />
        <StatTile
          icon={ShieldCheck}
          label="Air gap"
          value={`${data?.air_gap.raw_keys_exposed ?? 0} keys exposed`}
          hint={`${data?.air_gap.order_signing_tools ?? 0} order-signing tools in Ring 3`}
        />
      </div>

      {SILO_ORDER.map((siloId) => {
        const rows = venues.filter((row) => row.silo === siloId)
        if (rows.length === 0) return null
        const silo = (data?.silos ?? []).find((entry) => entry.silo === siloId)
        return (
          <Card key={siloId} className="border-border/70 bg-card/60 shadow-none">
            <CardHeader className="pb-2">
              <CardTitle className="flex flex-wrap items-center gap-2 text-sm">
                {siloId}
                <Badge variant="outline" className="font-mono text-[9px]">
                  {silo?.market ?? '—'}
                </Badge>
                <span className="font-mono text-[10px] font-normal text-muted-foreground">
                  {silo?.configured ?? 0}/{rows.length} configured
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent className="px-0">
              <div className="overflow-x-auto">
                <table className="w-full min-w-[900px] text-[11px]">
                  <thead className="border-y border-border/60 text-[9px] uppercase tracking-wider text-muted-foreground">
                    <tr>
                      <th className="px-3 py-1.5 text-left">Venue</th>
                      <th className="px-3 py-1.5 text-left">Adapter</th>
                      <th className="px-3 py-1.5 text-left">Credential</th>
                      <th className="px-3 py-1.5 text-left">Masked key</th>
                      <th className="px-3 py-1.5 text-left">Ed25519</th>
                      <th className="px-3 py-1.5 text-left">Feed</th>
                      <th className="px-3 py-1.5 text-left">Settlement</th>
                      <th className="px-3 py-1.5 text-right">Probe</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono">
                    {rows.map((row) => (
                      <tr key={row.venue} className="border-b border-border/30">
                        <td className="px-3 py-1 text-left text-foreground">{row.venue}</td>
                        <td className="px-3 py-1 text-left">
                          <span className="inline-flex items-center gap-1">
                            {row.adapter_importable ? (
                              <CheckCircle2 className="size-3 text-emerald-300" />
                            ) : (
                              <XCircle className="size-3 text-rose-300" />
                            )}
                            <span className="text-muted-foreground">
                              {row.adapter_module.split('.').slice(-3).join('.')}
                            </span>
                          </span>
                        </td>
                        <td className="px-3 py-1 text-left">
                          <span
                            className={
                              row.credential_status === 'CONFIGURED'
                                ? 'text-emerald-300'
                                : 'text-muted-foreground'
                            }
                          >
                            {row.credential_status}
                          </span>
                        </td>
                        <td className="px-3 py-1 text-left">
                          <span className="nios-masked-secret">
                            {row.api_key_masked ?? '—'}
                          </span>
                        </td>
                        <td className="px-3 py-1 text-left">
                          <span
                            className={
                              row.signing.dry_run_verified
                                ? 'text-emerald-300'
                                : 'text-amber-300'
                            }
                          >
                            {row.signing.dry_run_verified ? 'SIGNING READY' : 'UNVERIFIED'}
                          </span>
                        </td>
                        <td className="px-3 py-1 text-left text-muted-foreground">
                          {row.feed.mode} · {row.feed.health}
                        </td>
                        <td className="px-3 py-1 text-left text-muted-foreground">
                          {String(row.settlement.settlement_cycle ?? '—')} ·{' '}
                          {String(row.settlement.margin_regime ?? '—')}
                        </td>
                        <td className="px-3 py-1 text-right">
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            className="h-6 px-2 text-[10px]"
                            disabled={ping.pending && venue === row.venue}
                            onClick={() => probe(row.venue)}
                          >
                            <Radio className="mr-1 size-3" />
                            Ping
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>
        )
      })}

      <Card className="border-border/70 bg-card/60 shadow-none">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Interactive adapter &amp; signing probe</CardTitle>
        </CardHeader>
        <CardContent>
          <ActionFeedback pending={ping.pending} error={ping.error}>
            {ping.receipt ? (
              <div className="grid gap-2 text-[10px] sm:grid-cols-2 xl:grid-cols-4">
                <ProofRow label="Venue" value={ping.receipt.venue} />
                <ProofRow label="Silo" value={ping.receipt.silo} />
                <ProofRow
                  label="Adapter importable"
                  value={ping.receipt.adapter.importable ? 'true' : 'false'}
                />
                <ProofRow label="Probe verdict" value={ping.receipt.ok ? 'PASS' : 'FAIL'} />
                <ProofRow
                  label="Credential"
                  value={`${ping.receipt.credential.status} · ${ping.receipt.credential.api_key_masked ?? '—'}`}
                />
                <ProofRow
                  label="Raw secret returned"
                  value={ping.receipt.credential.raw_secret_returned ? 'true' : 'false'}
                />
                <ProofRow
                  label="Signing"
                  value={String(ping.receipt.signing.algorithm ?? 'Ed25519')}
                />
                <ProofRow
                  label="Signature bytes"
                  value={String(ping.receipt.signing.signature_length ?? '—')}
                />
                <ProofRow
                  label="Message SHA-256"
                  value={truncateDigest(String(ping.receipt.signing.message_sha256 ?? ''))}
                />
                <ProofRow
                  label="Fingerprint"
                  value={String(ping.receipt.signing.public_fingerprint ?? '—')}
                />
                <ProofRow
                  label="Latency budget"
                  value={`${ping.receipt.latency_budget_ms.toFixed(0)} ms`}
                />
                <ProofRow label="Operator" value={ping.receipt.operator} />
              </div>
            ) : (
              <p className="text-[11px] text-muted-foreground">
                Press “Ping” on any venue to import its adapter and prove the Ring 0 Ed25519
                signer round-trips. No secret ever leaves the vault.
              </p>
            )}
          </ActionFeedback>
        </CardContent>
      </Card>

      <DataState loading={loading} error={error}>
        <div className="rounded-lg border border-border/60 bg-background/40 px-3 py-2 text-[10px] text-muted-foreground">
          <p className="flex flex-wrap items-center gap-2">
            <ShieldCheck className="size-3 text-emerald-300" />
            Write-only vault at <span className="font-mono">{vault?.path ?? '—'}</span> · key
            version <span className="font-mono">{vault?.key_version ?? '—'}</span> · raw secrets
            exposed <span className="font-mono">{vault?.raw_secrets_exposed ? 'true' : 'false'}</span>
          </p>
          <p className="mt-1">
            {data?.law ?? 'sec_004 air gap - Ring 3 never reads a broker key.'}
          </p>
          <button
            type="button"
            className="mt-2 underline"
            onClick={reload}
            aria-label="Refresh the broker matrix"
          >
            Refresh matrix
          </button>
        </div>
      </DataState>
    </PageShell>
  )
}
