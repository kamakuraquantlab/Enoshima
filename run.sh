#!/usr/bin/env bash
# Run enoshima on a free-threaded Python, so a sweep's parameter sets run in
# parallel rather than taking turns on one core.
#
#   ./run.sh run --env EXAMPLE_BACKTEST --from 2026-06-01 --to 2026-06-30 --optimize grid
#
# Arguments go to `enoshima` unchanged. The interpreter is $ENOSHIMA_PYTHON, or
# the pyenv environment `enoshima-314t` (see README, "Running on free-threaded
# Python"). Refuses to start rather than run slowly: on a GIL build, or when an
# import turns the GIL back on, the run would finish -- on one core, with
# nothing to say so.
set -euo pipefail

PY="${ENOSHIMA_PYTHON:-}"
if [[ -z "$PY" ]]; then
    if ! prefix=$(pyenv prefix enoshima-314t 2>/dev/null); then
        echo "run.sh: no ENOSHIMA_PYTHON and no pyenv environment enoshima-314t" >&2
        exit 1
    fi
    PY="$prefix/bin/python"
fi

"$PY" - <<'EOF'
import sys
import sysconfig

if not sysconfig.get_config_var("Py_GIL_DISABLED"):
    sys.exit(f"run.sh: {sys.executable} is not a free-threaded build")

# Everything on a backtest's path. A C extension that does not declare
# free-threading support re-enables the GIL when imported -- pandas before 3.0
# does -- and that would silently serialise every thread.
import enoshima.cli, hase.bronze, numpy, pandas, pyarrow.parquet  # noqa: E401,F401

if sys._is_gil_enabled():
    sys.exit("run.sh: an import re-enabled the GIL; check the RuntimeWarning above")
EOF

exec "$PY" -m enoshima.cli "$@"
