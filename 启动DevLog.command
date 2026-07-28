#!/usr/bin/env bash
# DevLog 启动脚本（macOS / Linux）
set -e
cd "$(dirname "$0")"

PY=""
for c in python3 python; do
  if command -v $c >/dev/null 2>&1; then
    if $c -c 'import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)' 2>/dev/null; then
      PY=$c; break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "  [错误] 需要 Python 3.9 或更高版本"
  echo "  macOS:  brew install python3"
  echo "  Ubuntu: sudo apt install python3"
  exit 1
fi

exec $PY -m devlog "$@"
