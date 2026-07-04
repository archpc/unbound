#!/bin/bash
#
# serve.sh — serve this directory read-only on the LAN so the dashboard is
# viewable from any device without SSH-ing into the mini. Long-running
# (KeepAlive via local.unbound.dashboardserve), deliberately run as
# localadmin rather than root — a static file server needs no privilege,
# and this is the one long-lived, network-facing process in this repo.
#
# Bound to the LAN IP only, matching unbound's own listener scope (no
# 0.0.0.0, no auth — same trust posture as the resolver it's reporting on).

set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
BIND="${BIND:-192.168.0.100}"
PORT="${PORT:-8081}"

exec python3 -m http.server "$PORT" --bind "$BIND" --directory "$script_dir"
