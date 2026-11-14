/**
 * Toast bus.
 *
 * A module-level emitter plus one host mounted by the app root, so a page can
 * report an order rejection or a reconcile result without prop-drilling a
 * callback through the shells.
 */

import { useEffect, useState } from "react";
import type { ReactNode } from "react";

export type ToastTone = "info" | "success" | "error";

export interface ToastMessage {
  id: number;
  text: string;
  tone: ToastTone;
}

type Listener = (messages: ToastMessage[]) => void;

let messages: ToastMessage[] = [];
let nextId = 1;
const listeners = new Set<Listener>();

function emit(): void {
  const snapshot = [...messages];
  for (const listener of listeners) listener(snapshot);
}

export function toast(text: string, tone: ToastTone = "info"): void {
  const id = nextId++;
  messages = [...messages, { id, text, tone }].slice(-4);
  emit();
  window.setTimeout(() => {
    messages = messages.filter((message) => message.id !== id);
    emit();
  }, 5200);
}

export function dismissToast(id: number): void {
  messages = messages.filter((message) => message.id !== id);
  emit();
}

export function ToastHost(): ReactNode {
  const [current, setCurrent] = useState<ToastMessage[]>(messages);

  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent([...messages]);
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);

  if (current.length === 0) return null;

  return (
    <div className="nx-toasts" aria-live="polite" aria-atomic="false">
      {current.map((message) => (
        <output key={message.id} className={`nx-toast nx-toast--${message.tone}`}>
          <span className="nx-strong nx-xs">{message.tone.toUpperCase()}</span>
          <span className="nx-truncate" style={{ whiteSpace: "normal" }}>
            {message.text}
          </span>
        </output>
      ))}
    </div>
  );
}
