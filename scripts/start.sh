#!/usr/bin/env sh
# Thin wrapper: start the worker process and the local web UI.
cd "$(dirname "$0")/.." || exit 1
unset PYTHONPATH
if [ ! -x .venv/bin/python ]; then
    echo "Virtual environment not found. Run scripts/setup.sh first." >&2
    exit 1
fi
exec .venv/bin/python -m relaxbook.cli start "$@"
