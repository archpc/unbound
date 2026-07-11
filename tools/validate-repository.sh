#!/bin/bash
# Run every deterministic repository check that requires no production access.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYCACHE="$(mktemp -d "${TMPDIR:-/tmp}/unbound-validation.XXXXXX")"
trap 'rm -rf "$PYCACHE"' EXIT

cd "$ROOT"

python3 tools/check-docs.py
python3 -m json.tool deployment-manifest.json >/dev/null

for dashboard in config/grafana/dashboards/*.json; do
    python3 -m json.tool "$dashboard" >/dev/null
done

ruby -e '
require "yaml"
ARGV.each do |path|
  YAML.safe_load(
    File.read(path),
    permitted_classes: [],
    permitted_symbols: [],
    aliases: false
  )
end
' \
    config/prometheus/*.yml \
    config/grafana/provisioning/dashboards/*.yml \
    config/grafana/provisioning/datasources/*.yml

bash -n scripts/dns/*.sh scripts/maintenance/*.sh tools/*.sh
zsh -n scripts/dns/warm-cache.zsh
plutil -lint launchd/*.plist

PYTHONPYCACHEPREFIX="$PYCACHE" python3 -m py_compile \
    scripts/monitoring/*.py tools/*.py
PYTHONPYCACHEPREFIX="$PYCACHE" python3 -m unittest discover \
    -s tests -p 'test_*.py'

# Check committed content as well as worktree edits. A plain diff check is empty in a
# clean checkout, so compare HEAD with Git's empty tree to cover the full snapshot.
# root.hints is an externally maintained snapshot whose upstream format contains
# trailing spaces; its semantic validation remains an Unbound responsibility.
EMPTY_TREE="$(git hash-object -t tree /dev/null)"
git diff --check "$EMPTY_TREE" HEAD -- . ':(exclude)config/unbound/root.hints'
git diff --check

echo "offline repository validation passed"
