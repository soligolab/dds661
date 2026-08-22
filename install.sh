#!/usr/bin/env bash
set -euo pipefail

python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "Virtualenv created at .venv and dependencies installed."
