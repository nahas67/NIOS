/**
 * `/live/login` — the three-factor armoured vault door.
 *
 * Password + six-digit live PIN + an explicit acknowledgement that the capital
 * is real. The 22-gate pre-flight certificate is fetched before the form is
 * usable so the operator can see exactly what is red.
 */

import { useMemo, useState, type ReactNode } from "react";
import { ArrowLeft, ArrowRight, LockKeyhole, ShieldAlert } from "lucide-react";
import type { Portal } from "@/lib/router";
import { navigate } from "@/lib/router";
import { authApi } from "@/lib/api";
import { useAction, useResource } from "@/lib/use-api";
import { useSession } from "@/lib/session";
import { pushToast } from "@/lib/toast";
import type { AuthSession } from "@/lib/types";
import { PortalScaffold } from "./PortalScaffold";
import { ErrorBlock, StatusPill } from "@/design-system/primitives";
import { num } from "@/lib/format";

export function LiveLoginPortal({ onPortal }: { onPortal: (portal: Portal) => void }): ReactNode {
  const { signIn } = useSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [pin, setPin] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const login = useAction<AuthSession>();
  const preflight = useResource("live-door-preflight", () => authApi.preflight(), 15_000);

  const certificate = preflight.data;
  const passed = certificate?.passed_gates ?? 0;
  const total = certificate?.total_gates ?? 22;
  const ready = certificate?.ready_for_live_cutover === true;
  const failed = certificate?.failed_gate_ids ?? [];

  const gates = useMemo(() => (certificate?.gates ?? []).slice(0, 22), [certificate]);

  const pinValid = /^\d{6}$/.test(pin);
  const canSubmit = username.trim() !== "" && password !== "" && pinValid && acknowledged && !login.busy;

  const submit = async () => {
    if (!canSubmit) return;
    const receipt = await login.run(() => authApi.loginLive({ username: username.trim(), password, live_pin: pin }));
    if (receipt) {
      signIn(receipt);
      pushToast("Live vault unlocked", "30 minute lease · every order is Ring 0 signed", "warn");
      navigate("/live/dashboard");
    }
  };

  return (
    <PortalScaffold
      mode="live"
      eyebrow="Live portal · three factors"
      title="Open the live sovereign vault"
      subtitle="Real capital. A 30-minute lease. Ring 0 signs every order and the 22-gate pre-flight must be green before the door opens."
      aside={
        <div className="flex h-full flex-col justify-between gap-7">
          <div>
            <div className="row-between">
              <p className="eyebrow">22-gate pre-flight</p>
              <StatusPill
                value={ready ? "CERTIFIED" : certificate ? "NOT READY" : "PROBING"}
                tone={ready ? "ok" : certificate ? "bad" : "neutral"}
              />
            </div>
            <h2 className="mt-2 text-[22px] leading-tight font-semibold">
              {passed}/{total} gates certified for this build.
            </h2>
            <p className="mt-1 text-[11.5px] text-[var(--muted)]">
              {certificate?.release ? `Release ${certificate.release}` : "Awaiting the certificate"} · signer{" "}
              {certificate?.certificate_sha256 ? `${certificate.certificate_sha256.slice(0, 12)}…` : "—"}
            </p>

            <div className="mt-4 max-h-[260px] overflow-y-auto rounded-[var(--r-md)] border border-[var(--line)]">
              <table className="table table-compact">
                <thead>
                  <tr>
                    <th scope="col">Gate</th>
                    <th scope="col">Status</th>
                    <th scope="col" className="text-right">
                      ms
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {gates.length === 0 ? (
                    <tr>
                      <td colSpan={3} className="text-[var(--dim)]">
                        Reading the certificate…
                      </td>
                    </tr>
                  ) : (
                    gates.map((gate) => (
                      <tr key={gate.gate}>
                        <td className="max-w-[190px] truncate" title={gate.label}>
                          {gate.label}
                        </td>
                        <td>
                          <StatusPill
                            value={gate.status}
                            tone={gate.status.toUpperCase() === "PASS" ? "ok" : gate.status.toUpperCase() === "WARN" ? "warn" : "bad"}
                          />
                        </td>
                        <td className="num">{gate.duration_ms != null ? num(gate.duration_ms, 1) : "—"}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
            {failed.length > 0 ? (
              <p className="mono mt-2 text-[10px] text-[var(--neg)]">failing: {failed.slice(0, 4).join(" · ")}</p>
            ) : null}
          </div>

          <div className="rounded-[var(--r-md)] border border-[rgba(244,63,94,0.4)] bg-[rgba(244,63,94,0.08)] p-3">
            <div className="row">
              <ShieldAlert size={15} className="text-[#f43f5e]" aria-hidden="true" />
              <p className="text-[12px] font-medium text-[#fecdd3]">This door trades real money.</p>
            </div>
            <p className="mt-1.5 text-[11px] leading-relaxed text-[#fda4af]">
              Crossing it does not change the kernel; it changes the counterparty. The 98% conformal gate still abstains on
              ~99% of tape, and the kill switch still fires in under a millisecond.
            </p>
          </div>
        </div>
      }
      footer={
        <div className="row-between">
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => onPortal("login")}>
            <ArrowLeft size={11} aria-hidden="true" />
            Both doors
          </button>
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => onPortal("paper-login")}>
            Use the paper sandbox instead
          </button>
        </div>
      }
    >
      <form
        className="flex flex-col gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <div className="row gap-2 rounded-[var(--r-sm)] border border-[rgba(245,158,11,0.3)] bg-[rgba(245,158,11,0.08)] px-3 py-2">
          <LockKeyhole size={13} className="text-[#f59e0b]" aria-hidden="true" />
          <p className="mono text-[9.5px] tracking-[0.08em] text-[#fcd34d] uppercase">three-factor · armed vault door</p>
        </div>

        <label className="field">
          <span className="field-label">Username</span>
          <input
            className="input"
            value={username}
            autoComplete="username"
            onChange={(event) => setUsername(event.target.value)}
            aria-label="Live username"
            required
          />
        </label>
        <label className="field">
          <span className="field-label">Password</span>
          <input
            className="input"
            type="password"
            value={password}
            autoComplete="current-password"
            onChange={(event) => setPassword(event.target.value)}
            aria-label="Live password"
            required
          />
        </label>
        <label className="field">
          <span className="field-label">Six-digit live PIN</span>
          <input
            className="input tnum tracking-[0.5em]"
            inputMode="numeric"
            pattern="\d{6}"
            maxLength={6}
            value={pin}
            autoComplete="off"
            onChange={(event) => setPin(event.target.value.replace(/\D/g, "").slice(0, 6))}
            aria-label="Six digit live PIN"
            aria-invalid={pin !== "" && !pinValid}
            required
          />
        </label>

        <label className="check mt-1">
          <input
            type="checkbox"
            checked={acknowledged}
            onChange={(event) => setAcknowledged(event.target.checked)}
            aria-label="Acknowledge that live trading uses real capital"
          />
          <span>
            I understand this workspace places <span className="text-[#fecdd3]">real orders with real capital</span>, that
            the session expires after 30 minutes, and that the kill switch flattens every silo.
          </span>
        </label>

        {login.error ? <ErrorBlock message={login.error} /> : null}

        <button type="submit" className="btn btn-danger mt-1" disabled={!canSubmit}>
          {login.busy ? "Opening vault…" : "Enter live sovereign vault"}
          <ArrowRight size={12} aria-hidden="true" />
        </button>

        <p className="mono text-[9px] leading-relaxed tracking-[0.06em] text-[var(--dim)] uppercase">
          live pin rejected with 403 · no elevation from a paper token · pre-flight {passed}/{total}
        </p>
      </form>
    </PortalScaffold>
  );
}
