import { Component, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryState {
  error: Error | null;
}

/** Keep one crashing workstation from blanking the whole cockpit. */
export class ErrorBoundary extends Component<{ children: ReactNode }, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    globalThis.console?.error("NIOS workstation fault", error, info.componentStack);
  }

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="flex min-h-screen items-center justify-center bg-canvas p-6 text-ink">
        <div className="w-full max-w-lg rounded-[6px] border border-bear-border bg-surface p-4">
          <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-bear">WORKSTATION FAULT</div>
          <p className="mt-2 text-[12px] leading-relaxed text-ink-2">
            This panel failed to render. The backend session is unaffected — every control remains reachable from the sidebar.
          </p>
          <pre className="nios-scroll mt-3 max-h-40 rounded-[4px] border border-hairline bg-canvas p-2 font-mono text-[10px] text-ink-3">
            {error.message}
          </pre>
          <button
            type="button"
            onClick={() => this.setState({ error: null })}
            className="mt-3 h-7 rounded-[4px] border border-hairline bg-elevated px-2.5 text-[10px] font-medium uppercase tracking-[0.1em] text-ink-2 hover:text-ink"
          >
            Retry panel
          </button>
        </div>
      </div>
    );
  }
}
