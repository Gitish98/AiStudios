#!/usr/bin/env bash
# Cron wrapper for one paper cycle. Resolves the repo dir regardless of where
# cron invokes it, logs are handled by the crontab redirect (see docs/10 §7).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
mkdir -p logs

PY="$(command -v python3 || command -v python)"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] run-cycle starting"
"$PY" cli.py run-cycle
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] run-cycle done"
