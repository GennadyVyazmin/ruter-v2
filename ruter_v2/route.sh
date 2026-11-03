#!/usr/bin/env bash
set -Eeuo pipefail

# Installed wrapper is /opt/ruter-v2/route.sh; repository wrapper is ruter_v2/route.sh.
DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ -f "$DIR/ruter_v2/routing.py" ]]; then
  exec python3 "$DIR/ruter_v2/routing.py" "$@"
fi
exec python3 "$DIR/routing.py" "$@"
