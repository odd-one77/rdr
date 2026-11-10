#!/usr/bin/env bash
# Linux / macOS launcher (same behaviour as RDR.bat).
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
exec .venv/bin/python run.py "$@"
