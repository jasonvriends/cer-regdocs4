#!/usr/bin/env bash
# Create .venv and install what ingest.py and scout.py need.
# docling brings torch; ingest.py uses a CUDA GPU when one with enough memory is
# present (about 4x faster on a 4090) and falls back to CPU otherwise.
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
echo "venv ready: .venv/bin/python scout.py --help, .venv/bin/python ingest.py --help"
