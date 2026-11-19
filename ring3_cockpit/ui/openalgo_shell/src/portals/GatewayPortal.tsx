/**
 * `/login` — the dual-portal gateway.
 *
 * Two architectural doors, never one generic form: PAPER is a sandbox, LIVE is
 * a vault.  The choice is explicit before any credential is typed.
 */

import { Badge } from "../components/ui";
import { Icon } from "../components/Icon";
import { useSession } from "../state/session";
import { useSearchParams } from "../lib/router";
import { PortalFrame } from "./PortalFrame";

function Door({
  tone,
  title,
  eyebrow,
  description,
  bullets,
  href,
  cta,
  icon,
}: {
  tone: "paper" | "live";
  title: string;
  eyebrow: string;
  description: string;
  bullets: string[];
  href: string;
  cta: string;
  icon: "play" | "lock";
}) {
  const live = tone === "live";
  return (
    <a
      href={href}
      className={`group flex min-h-[220px] flex-col justify-between rounded-[6px] border p-4 transition-colors ${
        live ? "border-bear-border bg-bear-tint hover:border-[#6A3538]" : "border-hairline bg-surface hover:border-[#3A3D44]"
      }`}
    >
      <div>
        <div className="flex items-center justify-between">
          <span className={`text-[9px] font-semibold uppercase tracking-[0.2em] ${live ? "text-bear" : "text-accent"}`}>{eyebrow}</span>
          <Icon name={icon} size={15} className={live ? "text-bear" : "text-accent"} />
        </div>
        <h2 className="mt-2 text-[16px] font-semibold leading-tight text-ink">{title}</h2>
        <p className="mt-1.5 text-[11px] leading-relaxed text-ink-2">{description}</p>
        <ul className="mt-3 space-y-1">
          {bullets.map((bullet) => (
            <li key={bullet} className="flex items-start gap-1.5 text-[10px] leading-snug text-ink-3">
              <span className={`mt-[6px] h-[3px] w-[3px] shrink-0 ${live ? "bg-bear" : "bg-accent"}`} />
              {bullet}
            </li>
          ))}
        </ul>
      </div>
      <div
        className={`mt-4 flex h-8 items-center justify-center gap-2 rounded-[4px] border text-[10px] font-semibold uppercase tracking-[0.14em] ${
          live ? "border-bear bg-bear text-[#0D0E10]" : "border-accent bg-accent text-[#0D0E10]"
        }`}
      >
        {cta}
        <Icon name="arrowRight" size={12} />
      </div>
    </a>
  );
}

export function GatewayPortal() {
  const { session } = useSession();
  const params = useSearchParams();
  const reason = params.get("reason");

  return (
    <PortalFrame
      eyebrow="Sovereign Gateway"
      title="Choose your execution environment"
      subtitle="The terminal has two doors. One is a simulator with no counterparty; the other signs real orders against real venues. They never share credentials."
      width={720}
      footer={
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-[4px] border border-hairline bg-surface px-3 py-2">
          <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.12em] text-ink-3">
            <Icon name="info" size={12} />
            SESSION
            <span className="text-ink-2">{session?.authenticated ? `${session.mode} · ${session.username ?? "operator"}` : "NONE"}</span>
          </div>
          <div className="flex items-center gap-2">
            <Badge tone={session?.mode === "LIVE" ? "bear" : session?.mode === "PAPER" ? "accent" : "muted"}>{session?.mode ?? "UNSIGNED"}</Badge>
            {session?.mode === "PAPER" ? <Badge tone="neutral">RESUME /paper/dashboard</Badge> : null}
            {session?.mode === "LIVE" ? <Badge tone="bear">RESUME /live/dashboard</Badge> : null}
          </div>
        </div>
      }
    >
      {reason !== null ? (
        <div className="mb-3 rounded-[4px] border border-hairline bg-canvas px-2.5 py-2 text-[10px] uppercase tracking-[0.1em] text-ink-3">
          {reason}
        </div>
      ) : null}
      <div className="grid gap-3 md:grid-cols-2">
        <Door
          tone="paper"
          eyebrow="Door 01 · Sandbox"
          title="Paper Sandbox"
          description="Two-factor session against a simulated tape. Shadow executions, nursery graduation and tax-net backtests only."
          bullets={[
            "Username + password",
            "Simulated NAV, zero counterparty",
            "Autopilot, deposit and reset permitted",
            "30 L2 shadow fills before graduation",
          ]}
          href="/paper/login"
          cta="Enter Paper Sandbox"
          icon="play"
        />
        <Door
          tone="live"
          eyebrow="Door 02 · Vault"
          title="Live Sovereign Vault"
          description="Three-factor session against real venues. Every order is signed by Ring 0 after a 23-gate pre-flight certificate."
          bullets={[
            "Username + password + 6-digit live PIN",
            "Real capital, real statutory costs",
            "Kill switch and vault lock only",
            "SEBI algo-id, 10 OPS, static egress",
          ]}
          href="/live/login"
          cta="Enter Live Vault"
          icon="lock"
        />
      </div>
      <div className="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-hairline pt-3 text-[10px] text-ink-3">
        <span>
          First run? <a className="text-accent hover:text-ink" href="/setup">Provision the sovereign operator</a>
        </span>
        <span className="uppercase tracking-[0.14em]">Clean slate · 0 balance · 4 silos</span>
      </div>
    </PortalFrame>
  );
}
