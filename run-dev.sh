#!/usr/bin/env bash
set -u

cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

if [[ -x .venv/Scripts/python.exe ]]; then
  python_bin=".venv/Scripts/python.exe"
elif [[ -x .venv/bin/python ]]; then
  python_bin=".venv/bin/python"
else
  echo "Virtual environment not found. Create .venv and install requirements.txt first." >&2
  exit 1
fi

if ! command -v node >/dev/null 2>&1 || [[ ! -f frontend/node_modules/vite/bin/vite.js ]]; then
  echo "Node.js or frontend dependencies are missing. Run: npm install --prefix frontend" >&2
  exit 1
fi

cleanup() {
  trap - EXIT INT TERM
  kill "$backend_pid" "$frontend_pid" 2>/dev/null || true
  wait "$backend_pid" "$frontend_pid" 2>/dev/null || true
}

"$python_bin" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 &
backend_pid=$!
node frontend/node_modules/vite/bin/vite.js frontend --host 127.0.0.1 --config frontend/vite.config.ts &
frontend_pid=$!
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "Backend:  http://127.0.0.1:8000"
echo "Frontend: http://127.0.0.1:5173 (or the port printed by Vite)"
echo "Press Ctrl+C to stop both servers."
wait -n "$backend_pid" "$frontend_pid"
status=$?
if (( status != 0 )); then
  echo "A server exited with status $status; stopping the other server." >&2
fi
exit "$status"
