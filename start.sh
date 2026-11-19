#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [[ ! -x .venv/bin/python ]]; then
  python_cmd=${PYTHON_BIN:-}
  if [[ -z "$python_cmd" ]]; then
    for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
      if "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        python_cmd=$candidate
        break
      fi
    done
  fi
  if [[ -z "$python_cmd" ]] || ! "$python_cmd" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    echo '需要 Python 3.11 或更高版本。可以用 PYTHON_BIN 指定 Python 路径。' >&2
    exit 1
  fi
  "$python_cmd" -m venv .venv
fi

.venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt
if [[ ${1:-} == --verify-demo ]]; then
  shift
  exec .venv/bin/python -m benchmarks.smoke_http --standalone "$@"
fi
if [[ ${1:-} == --build-cloudflare ]]; then
  exec .venv/bin/python -m cloudflare.build_demo
fi
exec .venv/bin/python app.py "$@"
