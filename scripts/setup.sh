#!/usr/bin/env sh
# Thin wrapper: create/reuse .venv, install dependencies, write default config.
cd "$(dirname "$0")/.." || exit 1
unset PYTHONPATH
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
exec "$PY" -m relaxbook.cli setup "$@"
