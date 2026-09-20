#!/usr/bin/env bash
# Create .venv and install docling (CPU-only — no CUDA/torch-gpu needed).
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
echo "venv ready: .venv/bin/python ingest.py --help"
