#!/bin/bash
# Start the backend with graceful-shutdown timeout so Ctrl+C never orphans
# the process on port 8000 (the frontend auto-reconnects its WebSocket,
# which otherwise makes uvicorn's graceful shutdown hang forever).
cd "$(dirname "$0")"

# Prefer the reproducible project-local EDA toolchain when installed.
project_root=$(cd .. && pwd)
if [ -d "$project_root/.tools/eda/bin" ]; then
    export PATH="$project_root/.tools/eda/bin:$PATH"
fi
if [ -d "$project_root/.tools/eda/lib" ]; then
    export LD_LIBRARY_PATH="$project_root/.tools/eda/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export TCL_LIBRARY="$project_root/.tools/eda/lib/tcl8.6"
fi
if [ -d "$PWD/.venv/bin" ]; then
    export PATH="$PWD/.venv/bin:$PATH"
fi

# Kill any leftover process still holding port 8000 (orphaned uvicorn, etc.)
if command -v fuser >/dev/null 2>&1; then
    fuser -k 8000/tcp 2>/dev/null && sleep 1
elif command -v lsof >/dev/null 2>&1; then
    pids=$(lsof -ti:8000) && kill -9 $pids 2>/dev/null && sleep 1
fi

exec .venv/bin/python -m uvicorn main:app --reload --timeout-graceful-shutdown 3 --host 0.0.0.0 --port 8000
