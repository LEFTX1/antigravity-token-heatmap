#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/LEFTX1/antigravity-token-heatmap.git"
PLUGIN_ROOT="${HOME}/.gemini/config/plugins"
TARGET_DIR="${PLUGIN_ROOT}/token-heatmap"

echo ">>> Installing Antigravity Token Heatmap Cockpit..."
mkdir -p "${PLUGIN_ROOT}"

if [ -d "${TARGET_DIR}/.git" ]; then
  echo ">>> Updating existing installation in ${TARGET_DIR}..."
  git -C "${TARGET_DIR}" pull --ff-only
elif [ -d "${TARGET_DIR}" ]; then
  BAK_DIR="${TARGET_DIR}.bak.$(date +%Y%m%d%H%M%S)"
  echo ">>> Backing up existing folder to ${BAK_DIR}..."
  mv "${TARGET_DIR}" "${BAK_DIR}"
  git clone --depth 1 "${REPO_URL}" "${TARGET_DIR}"
  if [ -d "${BAK_DIR}/data" ]; then
    cp -R "${BAK_DIR}/data" "${TARGET_DIR}/data"
  fi
elif command -v agy >/dev/null 2>&1; then
  echo ">>> Installing via Antigravity CLI (agy)..."
  agy plugin install "https://github.com/LEFTX1/antigravity-token-heatmap"
else
  echo ">>> Cloning repository into ${TARGET_DIR}..."
  git clone --depth 1 "${REPO_URL}" "${TARGET_DIR}"
fi

PY_BIN=""
if command -v python3 >/dev/null 2>&1; then
  PY_BIN="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PY_BIN="$(command -v python)"
fi

if [ -n "${PY_BIN}" ]; then
  echo ">>> Checking Python MCP SDK dependency using ${PY_BIN}..."
  if ! "${PY_BIN}" -c "import mcp" >/dev/null 2>&1; then
    echo ">>> Installing mcp>=1.2.0..."
    "${PY_BIN}" -m pip install --upgrade "mcp>=1.2.0" || "${PY_BIN}" -m pip install --user --upgrade "mcp>=1.2.0"
  fi
  if ! command -v python >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1 && [ -f "${TARGET_DIR}/mcp_config.json" ]; then
    echo ">>> Updating mcp_config.json to use python3..."
    "${PY_BIN}" -c "
import json, pathlib
p = pathlib.Path('${TARGET_DIR}/mcp_config.json')
cfg = json.loads(p.read_text(encoding='utf-8'))
cfg['mcpServers']['heatmap']['command'] = 'python3'
p.write_text(json.dumps(cfg, indent=2) + '\n', encoding='utf-8')
"
  fi
else
  echo "WARNING: Python 3.10+ not found on PATH. Install Python 3.10+ and run: pip3 install mcp>=1.2.0"
fi

echo ""
echo "Installation complete! Installed at: ${TARGET_DIR}"
echo "Reload plugins in Antigravity (or restart IDE), then prompt: 打开我的 Token 热力图"
