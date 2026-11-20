# NIOS V2.5 — `qa_human_tester.py`

A reusable, headless-human Playwright driver. It drives the real cockpit as a
trader would and records **one ledger row per defect**. It never prints or
persists the live API key.

## Usage

```powershell
$PY = "C:\Users\nahas\AppData\Local\Temp\opencode\nios-venv\Scripts\python.exe"
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = 1

# Structural only — no server, no browser:
& $PY scripts/qa_human_tester.py --offline

# Full run with live LLM (recommended: isolated port so state is guaranteed fresh):
& $PY scripts/qa_human_tester.py --live-llm `
    --api-file "C:\Users\nahas\OneDrive\Desktop\opencode-g0-api.txt" `
    --base-url "http://127.0.0.1:8011"

# Full run on the documented port 8001:
& $PY scripts/qa_human_tester.py --live-llm `
    --api-file "C:\Users\nahas\OneDrive\Desktop\opencode-g0-api.txt"
```

Options: `--offline`, `--live-llm`, `--api-file PATH`, `--base-url URL`,
`--report-dir DIR`, `--headed`, `--max-seconds N`.

## What it checks

Structural (always): 56-route census (17 workstations × 3 prefixes + 5
portals), route wiring in `App.tsx`, forbidden flat-matte CSS class forms,
expected operator labels, and literal `NaN`/`undefined`/`[object Object]` in
JSX text.

Browser (unless `--offline`): every `pageerror`, `console.error`, `requestfailed`,
unexpected 4xx/5xx (with an explicit whitelist for intentional errors such as
`422 INFEASIBLE_MISSION`, `LIVE_ELEVATION_REQUIRED`, wrong-password/invalid-PIN,
CSRF/guard denials and `/favicon.ico`), horizontal overflow at **1440×900 and
1280×800**, spinners stuck >10 s, rendered bad values, and **dead buttons** (a
visible click producing no DOM change, no network call and no URL/modal change).

Workflows: auth (`/setup` weak/mismatched PIN → clean-slate operator → 0
balance, dual doors, paper 2FA, live 3FA + risk ack + TTL, kill switch rearm,
vault lock, CSRF logout), all 17 workstations and their detailed interactions,
plus live-shell guardrail checks (no deposit/reset/autopilot/shadow-burst
controls in the LiveShell DOM).

## Secret safety

The API key file is **never read into the report or console**. `--api-file` is
resolved to a path and passed to the spawned NIOS runtime through
`NIOS_LLM_API_FILE`; the app reads and masks the key itself. The script learns
the key token only in-process to compute `***last4` and to scrub every string
that reaches a log or report. Console output shows only the mask.

## State isolation

A full run spawns `scripts/run_nios.py --workspace <temp>` with a throwaway
`NIOS_OPERATOR_AUTH_DIR`, so setup provisions a fresh operator at 0 balance.
If the target port already hosts a NIOS server, the driver attaches but records
a `blocked` row and an `environment_warning` — auth-dependent findings from
that run are environment-dependent.

## Report & exit codes

Writes `qa_report.json` and `qa_report.md` to a fresh temp dir (override with
`--report-dir`). Each defect row:
`{workflow, step, selector, symptom, root_cause_guess, evidence, severity}`.

- `0` — no defects
- `1` — at least one defect
- `2` — server/browser unavailable or boot failure (report still produced)

If the live `hy4-flash-free` endpoint is blocked, only those sub-steps are
marked `blocked` (not `defect`); the structural + UI report is still complete.

## Known environment blocker (observed 2026-11-20)

Starting a clean server from HEAD currently fails before binding the port:

```
ring2_brain/cognition/committee_graph.py:44
from core.llm_model_policy import ALLOWED_MODEL, is_live_enabled
ImportError: cannot import name 'is_live_enabled' from 'core.llm_model_policy'
```

`is_live_enabled` is defined in `core/llm_api_config.py`, not
`core/llm_model_policy.py`. This is outside this script's lane and is **not**
patched here; the driver records it verbatim as a `SERVER/boot-timeout` defect
with the server-log tail as evidence.
