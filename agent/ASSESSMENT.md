# Repository assessment for the read-only agent experiment

The repository is intended state for a live Unbound and monitoring stack, not evidence
of current runtime state. The prototype therefore keeps direct observations, historical
metrics, documentation, and deployment comparison as distinct evidence sources.

## Existing components reused

- `tools/verify-deployment.py` is the canonical stdlib-only, read-only repo-to-runtime
  comparison and remains the only implementation used by `deployment_drift`.
- Prometheus already exposes the resolver, exporter, upstream, RPZ, load, and memory
  evidence required for the first diagnostic questions. The tool uses exact named
  expressions derived from `config/prometheus/prometheus.yml`, its rules, and dashboards.
- `config/prometheus/blackbox.yml` defines the existing external-resolution and RPZ
  canaries. The direct DNS tool mirrors the documented checks against `127.0.0.1`.
- LaunchDaemon labels and log paths come from the tracked plist definitions. Status is
  normalized from `launchctl print`; no service-management verbs exist in the registry.
- Canonical documentation is already explicitly enumerated by repository policy. The
  documentation tool searches that fixed list deterministically.
- The repository uses Python 3.9-compatible standard library code, `unittest`, and the
  offline `tools/validate-repository.sh` entrypoint. The prototype follows those choices.

## Important boundary discovered

The Unbound control socket is root-owned and the existing exporter runs as root solely
to read it. This gateway refuses root execution and never invokes `sudo`. Consequently,
`unbound_stats` may report unavailable unless a separately reviewed, least-privilege
read path is established. Prometheus is the normal unprivileged source for those
aggregates in this prototype. The experiment does not change socket ownership or ACLs.

## Deliberate non-reuse

Routine-operation scripts can update blocklists, reload zones, warm caches, or otherwise
change runtime state. They are intentionally not exposed. The gateway contains no generic
script wrapper, shell tool, SSH client tool, deployment tool, or service action tool.

## Evaluation boundary

The evaluation harness reuses the production orchestrator, schemas, prompt, and tool
definitions through injected model and handler dependencies. Its 40 scenarios use only
synthetic evidence. Optional real-model mode exercises the configured loopback model but
keeps the synthetic registry, so model evaluation cannot become a live mini probe.
