#!/usr/bin/env bash
# Bank Journal BI —— macOS / Linux 启动脚本
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

if [ ! -f data/journal.db ]; then
  echo "[init] 未找到账本，正在生成演示数据..."
  "$PY" tools/gen_demo_data.py
fi

echo "============================================"
echo "  Bank Journal BI   http://127.0.0.1:8765"
echo "  Ctrl+C 停止。数据保存在 data/ 目录。"
echo "============================================"
NO_BROWSER="${NO_BROWSER:-0}" exec "$PY" app.py
