#!/usr/bin/env bash
# =============================================================================
#  NIOS - 1-click launch (Linux / macOS / WSL)
#
#  1. Resolves a Python interpreter (NIOS_PYTHON > NIOS_VENV > .venv > venv
#     > the pre-provisioned dev venv (WSL) > python3.12 > python3).
#  2. Selects a bindable port.  If NIOS_PORT was forced it is honoured or the
#     launch fails loudly; otherwise a busy 8000 falls back to the first free
#     port in 8000..8010 (Operator Note #2) and the exact live URL is shown.
#  3. Runs the 17-gate release smoke test.  Any failure aborts the launch.
#  4. Starts the unified 4-ring production server
#     (REST + SSE + MCP + the compiled React 19 cockpit at /).
#
#  Usage:
#    ./start_nios.sh                 smoke test, then serve
#    ./start_nios.sh --skip-smoke    serve only
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

NIOS_HOST="${NIOS_HOST:-127.0.0.1}"
NIOS_PORT_WAS_SET="${NIOS_PORT:+1}"
NIOS_PORT="${NIOS_PORT:-8000}"
export NIOS_HOST

resolve_python() {
  local -a candidates=()
  local win_user="${USERNAME:-${USER:-}}"

  [ -n "${NIOS_PYTHON:-}" ] && candidates+=("$NIOS_PYTHON")
  [ -n "${NIOS_VENV:-}" ] && candidates+=("$NIOS_VENV/bin/python" "$NIOS_VENV/Scripts/python.exe")
  candidates+=(
    "$SCRIPT_DIR/.venv/bin/python"
    "$SCRIPT_DIR/.venv/Scripts/python.exe"
    "$SCRIPT_DIR/venv/bin/python"
    "$SCRIPT_DIR/venv/Scripts/python.exe"
  )
  # WSL / Git Bash view of the pre-provisioned dev venv on this workstation.
  if command -v cygpath >/dev/null 2>&1 && [ -n "${LOCALAPPDATA:-}" ]; then
    candidates+=("$(cygpath -u "$LOCALAPPDATA")/Temp/opencode/nios-venv/Scripts/python.exe")
  fi
  if [ -n "$win_user" ]; then
    candidates+=(
      "/mnt/c/Users/$win_user/AppData/Local/Temp/opencode/nios-venv/Scripts/python.exe"
      "/c/Users/$win_user/AppData/Local/Temp/opencode/nios-venv/Scripts/python.exe"
    )
  fi

  local candidate
  for candidate in "${candidates[@]}"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  for candidate in python3.12 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      printf '%s\n' "$(command -v "$candidate")"
      return 0
    fi
  done
  return 1
}

PYTHON="$(resolve_python)" || {
  echo
  echo "[NIOS] FATAL: no Python interpreter found."
  echo "       Create a virtualenv and point NIOS_VENV at it, e.g."
  echo "           python3.12 -m venv .venv"
  echo "           export NIOS_VENV=\"\$PWD/.venv\""
  echo
  exit 1
}

# --- preflight: the interpreter must actually carry the NIOS runtime ---------
if ! "$PYTHON" -c "import fastapi, uvicorn, pydantic, numpy" >/dev/null 2>&1; then
  echo
  echo "[NIOS] FATAL: $PYTHON cannot import the NIOS runtime."
  echo "       Missing one of: fastapi, uvicorn, pydantic, numpy."
  echo "       Point NIOS_PYTHON at the project interpreter, e.g."
  echo "           NIOS_PYTHON=/path/to/venv/bin/python ./start_nios.sh"
  echo
  exit 1
fi

# --- 2. select a bindable port (Operator Note #2) -----------------------------
if [ -n "$NIOS_PORT_WAS_SET" ]; then
  if ! "$PYTHON" scripts/nios_port_check.py "$NIOS_HOST" "$NIOS_PORT" >/dev/null 2>&1; then
    echo
    echo "[NIOS] FATAL: $NIOS_HOST:$NIOS_PORT is already in use (NIOS_PORT was forced)."
    echo "       Free the port, or unset NIOS_PORT to auto-select 8000..8010."
    echo
    exit 1
  fi
else
  NIOS_PORT_REQUESTED="$NIOS_PORT"
  if ! NIOS_PORT="$("$PYTHON" scripts/nios_port_check.py "$NIOS_HOST" --scan 8000 8010)"; then
    echo
    echo "[NIOS] FATAL: no free port in 8000..8010 on $NIOS_HOST."
    echo
    exit 1
  fi
  if [ "$NIOS_PORT" != "$NIOS_PORT_REQUESTED" ]; then
    echo "[NIOS] port $NIOS_PORT_REQUESTED is busy -> auto-selected $NIOS_PORT"
  fi
fi
export NIOS_PORT

echo
echo " NIOS =========================================================="
echo "  interpreter : $PYTHON"
echo "  cockpit     : http://$NIOS_HOST:$NIOS_PORT"
echo " ==============================================================="

if [ "${1:-}" != "--skip-smoke" ]; then
  echo
  echo "[NIOS] 1/2  Release smoke test (17 gates)..."
  echo
  if ! "$PYTHON" scripts/run_nios.py --smoke-test --host "$NIOS_HOST" --port "$NIOS_PORT"; then
    echo
    echo "[NIOS] FATAL: smoke test failed. Refusing to launch a non-certified build."
    echo
    exit 1
  fi
fi

echo
echo "[NIOS] 2/2  Launching unified production server on http://$NIOS_HOST:$NIOS_PORT"
echo "       Open the cockpit in a browser, or <Ctrl+C> to stop."
echo
exec "$PYTHON" scripts/run_nios.py --host "$NIOS_HOST" --port "$NIOS_PORT"
