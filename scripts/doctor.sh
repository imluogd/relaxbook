#!/usr/bin/env sh
# Thin wrapper: check the local environment item by item.
cd "$(dirname "$0")/.." || exit 1
unset PYTHONPATH
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
exec "$PY" -m relaxbook.cli doctor "$@"
