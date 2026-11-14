/**
 * Tiny toast bus.
 *
 * Operator receipts are proof artefacts, so every action result can be pinned
 * into a stack without a modal. The bus is a module-level event so a page never
 * has to thread a callback through the shells.
 */

import { useEffect, useState, type ReactNode } from "react";

export type ToastTone = "ok" | "warn" | "bad" | "info";

export interface Toast {
  id: number;
  title: string;
  detail?: string;
  tone: ToastTone;
}

const TOAST_EVENT = "nios:toast";
let nextId = 1;

export function pushToast(title: string, detail?: string, tone: ToastTone = "info"): void {
  window.dispatchEvent(
    new CustomEvent<Toast>(TOAST_EVENT, {
      detail: { id: nextId++, title, detail, tone },
    }),
  );
}

export function toastError(title: string, error: string | null): void {
  pushToast(title, error ?? "unknown failure", "bad");
}

export function ToastHost(): ReactNode {
  const [items, setItems] = useState<Toast[]>([]);

  useEffect(() => {
    const onToast = (event: Event) => {
      const toast = (event as CustomEvent<Toast>).detail;
      if (!toast) return;
      setItems((prev) => [...prev.slice(-3), toast]);
      window.setTimeout(() => {
        setItems((prev) => prev.filter((item) => item.id !== toast.id));
      }, 5200);
    };
    window.addEventListener(TOAST_EVENT, onToast);
    return () => window.removeEventListener(TOAST_EVENT, onToast);
  }, []);

  if (items.length === 0) return null;

  return (
    <div
      className="fixed right-4 bottom-4 z-[120] flex w-[min(420px,calc(100vw-2rem))] flex-col gap-2"
      role="status"
      aria-live="polite"
    >
      {items.map((item) => (
        <div key={item.id} className="toast">
          <div className="row-between">
            <span className={`pill ${item.tone === "ok" ? "pill-ok" : item.tone === "bad" ? "pill-bad" : item.tone === "warn" ? "pill-warn" : "pill-info"}`}>
              <span className="pill-dot" aria-hidden="true" />
              {item.tone}
            </span>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => setItems((prev) => prev.filter((row) => row.id !== item.id))}
              aria-label="Dismiss notification"
            >
              Dismiss
            </button>
          </div>
          <p className="mt-1.5 text-[12.5px] text-[var(--ink)]">{item.title}</p>
          {item.detail ? (
            <p className="mono mt-0.5 text-[10.5px] break-words text-[var(--dim)]">{item.detail}</p>
          ) : null}
        </div>
      ))}
    </div>
  );
}
