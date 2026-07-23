#!/usr/bin/env bash
# Execute only from the isolated Ubuntu/Wine MT5 service account.
set -euo pipefail

: "${MT5_WINDOWS_PYTHON:?MT5_WINDOWS_PYTHON must point to Wine Python}"
: "${WINE_BIN:=/usr/bin/wine64}"
: "${WINEPATH_BIN:=/usr/bin/winepath}"

app_dir="$(${WINEPATH_BIN} -w "${PWD}")"
exec "${WINE_BIN}" "${MT5_WINDOWS_PYTHON}" -m uvicorn mt5_sidecar_app:app \
  --app-dir "${app_dir}" --host 127.0.0.1 --port 8900 --no-access-log
