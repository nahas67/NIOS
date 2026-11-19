import { mkdirSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import react from "@vitejs/plugin-react";
import type { Plugin } from "vite";
import { defineConfig } from "vite";

const WORKSTATIONS = [
  "dashboard",
  "missions",
  "screener",
  "terminal",
  "orders",
  "options",
  "analyzer",
  "quant",
  "agents",
  "backtest",
  "brain",
  "forge",
  "ledger",
  "logbook",
  "verification",
  "brokers",
  "control",
];

/**
 * Final artefact scrubber.
 *
 * `postcss.flatness.mjs` removes elevation/blur/gradient declarations while the
 * stylesheet is still a PostCSS tree.  This plugin is the last line of defence:
 * it rewrites the emitted `dist/assets/*.css` so the shipped bundle can never
 * contain a shadow, a backdrop filter, a gradient, or a pill radius even if a
 * future dependency injects one.
 */
function flatnessScrub(): Plugin {
  return {
    name: "nios-flatness-scrub",
    apply: "build",
    generateBundle(_options, bundle) {
      for (const file of Object.values(bundle)) {
        if (file.type !== "asset" || !file.fileName.endsWith(".css")) continue;
        const source = String(file.source);
        const scrubbed = source
          .replace(/(?:-webkit-)?box-shadow\s*:[^;}]*;?/gi, "")
          .replace(/(?:-webkit-)?backdrop-filter\s*:[^;}]*;?/gi, "")
          .replace(/(?:linear|radial|conic)-gradient\s*\(/gi, "nios-flat-gradient(")
          .replace(/9999px/g, "8px");
        if (scrubbed !== source) file.source = scrubbed;
      }
    },
  };
}

/**
 * Terminal release manifest.
 *
 * `emptyOutDir` wipes `dist/` on every build, so the release manifest is
 * regenerated here after the bundle is written.  The statutory floors block is
 * the V2.1 accounting contract and stays byte-identical.
 */
function terminalManifest(): Plugin {
  return {
    name: "nios-terminal-manifest",
    apply: "build",
    closeBundle() {
      const assetsDir = join("dist", "assets");
      mkdirSync(assetsDir, { recursive: true });
      const files = readdirSync(assetsDir);
      const manifest = {
        schema_version: "v4.2.0.terminal",
        generated_by: "ring3_cockpit/ui/openalgo_shell/vite.config.ts",
        release: "v4.2.0-V2.2",
        frontend: "V2.2-INSTITUTIONAL-TERMINAL",
        javascript: files.filter((file) => file.endsWith(".js")).length,
        stylesheets: files.filter((file) => file.endsWith(".css")).length,
        css_framework: "Tailwind CSS v4",
        runtime: "React 19 + Vite",
        api_root: "/api/v2",
        shells: ["PaperShell", "LiveShell"],
        routes: 56,
        workstations: WORKSTATIONS.length,
        surfaces: {
          canvas: "#0D0E10",
          card: "#16181C",
          elevated: "#22242A",
          hairline: "#27292E",
        },
        contracts: {
          envelope: [
            "schema_version",
            "generated_at_ns",
            "clean_slate_active",
            "workspace_mode",
            "payload_sha256",
          ],
          risk_telemetry: [
            "gross_exposure_micros",
            "net_exposure_micros",
            "margin_used_micros",
            "free_margin_micros",
            "var_99_bps",
            "cvar_99_bps",
            "max_drawdown_bps",
            "realized_pnl_micros",
            "unrealized_pnl_micros",
            "total_tax_paid_micros",
          ],
          statutory_floors_bps: {
            INDIA_ZERODHA_DHAN: 8.5,
            CRYPTO_BINANCE: 16.0,
            US_IBKR: 2.8,
            FX_MT5: 1.6,
          },
        },
      };
      writeFileSync(
        join("dist", "headless-manifest.json"),
        `${JSON.stringify(manifest, null, 2)}\n`,
        "utf-8",
      );
    },
  };
}

export default defineConfig({
  base: "/",
  plugins: [react(), flatnessScrub(), terminalManifest()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2022",
    cssMinify: false,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8001",
        changeOrigin: true,
      },
    },
  },
});
