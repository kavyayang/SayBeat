#!/bin/zsh
set -eu
PROJECT_DIR="${0:A:h}"
PYTHON_BIN="${SYP_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in "$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" python3.13 python3.12 python3.11 python3; do
    resolved="$(command -v "$candidate" 2>/dev/null || true)"
    if [[ -n "$resolved" ]] && "$resolved" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PYTHON_BIN="$resolved"
      break
    fi
  done
fi
if [[ -z "$PYTHON_BIN" ]]; then
  print -u2 '需要 Python 3.11 或以上；可用 SYP_PYTHON 指定解释器路径。'
  exit 1
fi
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 or newer is required"'
ARGS=("$PROJECT_DIR/server.py" --port "${PORT:-8765}")
if [[ -n "${SYP_DATA_DIR:-}" ]]; then
  ARGS+=(--data-dir "$SYP_DATA_DIR")
fi
exec "$PYTHON_BIN" "${ARGS[@]}"
