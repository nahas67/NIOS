# Ring 3 UI — Headless Backend Mode (V1.6)

There is **no React, Vite, TypeScript, Biome or node_modules tree** in this
directory any more.  Release `v3.7.0-V1.6` ships a headless backend:

| Artifact | Purpose |
|---|---|
| `dist/index.html` | Deterministic, zero-JavaScript status document served on every client route (`/`, `/dashboard`, `/paper/*`, `/live/*`, …) with `200 text/html`. |
| `dist/headless-manifest.json` | Machine-readable contract: envelope keys, risk telemetry keys, statutory cost floors. |

The application surface is the REST/SSE API under `/api/v2`:

* `GET /api/v2/cockpit/health` — liveness, version, silo status.
* `GET /api/v2/cockpit/{state,pages,orders,options,terminal,analyzer,quant,agents,backtest,forge,ledger,logbook,brokers,mcp}`
  — the fifteen read-only telemetry payloads, each wrapped in the deterministic
  envelope (`schema_version`, `generated_at_ns`, `clean_slate_active`,
  `workspace_mode`, `payload_sha256`) plus per-silo risk telemetry.
* `GET /api/v2/workspace/{paper,live}/state` — mode-scoped workspace telemetry.
* `POST /api/v2/control/*` — the explicit control endpoints in `RUNBOOK.md`.

`ring3_cockpit/gateway/control_plane.py` still generates the same status
document in-process when `dist/index.html` is absent, so a client route never
404s even on a bare checkout.

Do not reintroduce a frontend framework without an explicit specification
change (`AGENTS.md` §7, §9.9).
