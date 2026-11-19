/**
 * `/live/login` — three-factor LIVE portal with explicit real-capital
 * acknowledgement and the live 23-gate pre-flight strip.
 */

import { useCallback, useEffect, useState } from "react";
import { Badge, Checkbox, Field, ProgressBar, TextInput, cx } from "../components/ui";
import { Icon } from "../components/Icon";
import { authLoginLive, errorMessage } from "../lib/api";
import { int } from "../lib/format";
import { gatesOf, numberAt, type GateRow } from "../lib/payload";
import { api } from "../lib/api";
import { navigateTo } from "../lib/router";
import { useSession } from "../state/session";
import { PortalError, PortalFrame, PortalSubmit } from "./PortalFrame";

export function LiveLoginPortal() {
  const { refresh } = useSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [livePin, setLivePin] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [gates, setGates] = useState<GateRow[]>([]);
  const [preflightSealed, setPreflightSealed] = useState(false);
  const [passed, setPassed] = useState(0);
  const [total, setTotal] = useState(23);

  const loadGates = useCallback(async () => {
    try {
      const payload = await api.get<unknown>("/control/preflight");
      setGates(gatesOf(payload));
      setPassed(numberAt(payload, ["passed_gates"], 0));
      setTotal(numberAt(payload, ["total_gates"], 23));
      setPreflightSealed(false);
    } catch {
      setPreflightSealed(true);
    }
  }, []);

  useEffect(() => {
    void loadGates();
  }, [loadGates]);

  const submit = async () => {
    setError(null);
    if (!/^[0-9]{4,12}$/.test(livePin)) {
      setError("The live PIN must be 4 to 12 digits.");
      return;
    }
    if (!acknowledged) {
      setError("You must acknowledge that this session can move real capital.");
      return;
    }
    setBusy(true);
    try {
      await authLoginLive(username.trim(), password, livePin);
      await refresh();
      navigateTo("/live/dashboard", { replace: true });
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <PortalFrame
      eyebrow="Door 02 · Live Sovereign Vault"
      title="Unseal the live vault"
      subtitle="Real capital. Every order is signed by Ring 0 only after the 23-gate certificate passes, the conformal gate clears and the SEBI algo-id is stamped."
      mode="live"
      width={470}
      footer={
        <div className="flex flex-wrap items-center justify-between gap-2 text-[10px] text-ink-3">
          <a href="/paper/login" className="inline-flex items-center gap-1 uppercase tracking-[0.12em] hover:text-ink-2">
            <Icon name="play" size={11} />
            Paper sandbox instead
          </a>
          <span className="inline-flex items-center gap-1 uppercase tracking-[0.12em] text-bear">
            <Icon name="lock" size={11} />
            kill switch armed
          </span>
        </div>
      }
    >
      <div className="mb-3 flex items-center gap-2 rounded-[4px] border border-bear-border bg-bear-tint px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-bear">
        <Icon name="warning" size={12} />
        REAL CAPITAL — ORDERS THAT FILL CANNOT BE UNDONE
      </div>
      <PortalError message={error} />
      <div className="space-y-3">
        <Field label="Operator Username">
          <TextInput value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" />
        </Field>
        <Field label="Password">
          <TextInput type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" />
        </Field>
        <Field label="6-Digit Live PIN" hint="Third factor. A missing PIN is an authorization verdict, not a validation error.">
          <TextInput
            value={livePin}
            onChange={(event) => setLivePin(event.target.value.replace(/[^0-9]/g, ""))}
            inputMode="numeric"
            maxLength={12}
            mono
            placeholder="000000"
          />
        </Field>

        <div className="rounded-[4px] border border-hairline bg-canvas p-2.5">
          <div className="mb-1.5 flex items-center justify-between text-[9px] uppercase tracking-[0.14em] text-ink-3">
            <span className="flex items-center gap-1.5">
              <Icon name="verification" size={11} />
              23-Gate Pre-Flight
            </span>
            <Badge tone={preflightSealed ? "muted" : passed >= total ? "bull" : "bear"}>
              {preflightSealed ? "SEALED" : `${int(passed)}/${int(total)}`}
            </Badge>
          </div>
          <ProgressBar value={preflightSealed ? 0 : passed} max={total} tone={passed >= total ? "bull" : "bear"} />
          {preflightSealed ? (
            <p className="mt-2 text-[10px] leading-snug text-ink-3">
              The certificate is session-guarded: it is re-issued and re-verified by Ring 0 at elevation. A sealed strip means
              this browser is not yet authenticated.
            </p>
          ) : (
            <div className="mt-2 max-h-24 space-y-[3px] overflow-auto">
              {gates.slice(0, 8).map((gate, index) => (
                <div key={`${gate.id}-${index}`} className="flex items-center justify-between gap-2 text-[9.5px]">
                  <span className="truncate text-ink-3">{gate.id}</span>
                  <span className={cx("shrink-0 font-semibold uppercase tracking-[0.1em]", gate.status === "PASS" ? "text-bull" : "text-bear")}>{gate.status}</span>
                </div>
              ))}
              {gates.length === 0 ? <div className="text-[10px] text-ink-3">No gate rows returned.</div> : null}
            </div>
          )}
        </div>

        <Checkbox
          checked={acknowledged}
          onChange={setAcknowledged}
          label={
            <span>
              I acknowledge that this session can place <span className="font-semibold text-bear">real orders</span> with real
              statutory costs, and that a fill is irreversible.
            </span>
          }
        />
        <PortalSubmit label={busy ? "Unsealing vault…" : "Unseal Live Vault"} onClick={submit} disabled={busy} />
      </div>
    </PortalFrame>
  );
}
