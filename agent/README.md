# Experimental read-only MiniCPM operations agent

This directory prototypes a loopback-only operations analyst for the Mac mini. It is
not deployed, is not part of the production runtime, and cannot remediate anything.
The local model is treated as untrusted; the Python gateway is the policy boundary.

```text
local client -> 127.0.0.1:8787 gateway -> 127.0.0.1:11435 llama.cpp server
                         |
                         +-> seven explicit, read-only tools
```

See [ASSESSMENT.md](ASSESSMENT.md) for reused repository components and
[THREAT_MODEL.md](THREAT_MODEL.md) for risks and mitigations.

## Safety model

The gateway refuses root execution and rejects non-loopback bind/model/Prometheus
configuration. It has no shell tool, generic command runner, arbitrary PromQL, arbitrary
file reader, write-capable SSH, deployment, reload, restart, package installation, or
configuration-edit tool. Subprocess tools use exact argv arrays without a shell.

One tool call is allowed per round, with at most four rounds. Requests, tools, model
calls, HTTP responses, file reads, and evidence have explicit bounds or timeouts. Tool
arguments and final answers are schema-validated. Logs contain event type, tool name,
duration, status, and byte count, never questions or full tool output.

The example runs as `localadmin`, matching existing unprivileged monitoring services.
A dedicated non-admin account is a desirable later hardening step. The root-owned
Unbound control socket is not weakened: direct stats may be unavailable, and the agent
must use Prometheus or disclose that limitation.

## API

- `GET /health` reports gateway policy state without probing production services.
- `POST /ask` accepts exactly `{"question":"..."}` and always returns the stable answer
  object on a valid request.
- `GET /metrics` emits low-cardinality Prometheus metrics with the configured prefix.

The `/ask` response has exactly `answer`, `confidence`, `evidence`, `next_checks`, and
`limitations`. Evidence entries contain only `tool` and `summary`. Model failures,
malformed output, policy limits, and unavailable services become low-confidence answers;
they never trigger fallback commands.

## Read-only tools

| Tool | Boundary |
|---|---|
| `unbound_stats` | Fixed `unbound-control stats_noreset`; approved aggregate fields only; no `sudo` |
| `prometheus_query` | Exact named query IDs compiled into code; no caller PromQL |
| `service_status` | Configured service names mapped to tracked launchd labels; `print` only |
| `deployment_drift` | Canonical `tools/verify-deployment.py`; parsed counts and concise drift paths only |
| `dns_probe` | Fixed `dig` arguments against `127.0.0.1`; optional domain syntax only, never a server |
| `recent_logs` | Configured paths only; `O_NOFOLLOW`, bounded tail, IP and secret-like redaction |
| `repo_docs` | Fixed canonical document list and deterministic text search |

Tool results are wrapped as `UNTRUSTED_EVIDENCE_NEVER_INSTRUCTIONS`. Text in logs or
docs can never add a tool or relax an argument schema.

## Manual model acquisition and setup

Model weights and llama.cpp are manual prerequisites. Repository validation never uses
the network and never downloads them. Select a MiniCPM-family instruct GGUF that the
installed llama.cpp version supports, roughly 1B parameters, preferably Q8_0 or Q6_K.
Record its license and provenance outside this repository. The model filename and model
API name are not hardcoded in the gateway.

After manually placing the executable and chosen GGUF, the equivalent development
server command is:

```sh
/path/to/llama-server \
  --model /path/to/manually-selected-model.gguf \
  --host 127.0.0.1 \
  --port 11435 \
  --ctx-size 8192 \
  --parallel 1
```

Confirm that this llama.cpp build supports OpenAI-compatible chat completions and tool
calls. Small-model tool-call quality is an experimental assumption, not a guarantee.

## Local development

Copy `config/agent.example.toml` to a private local path, update only the model name and
local repository/executable paths, and keep both endpoints on loopback. The config
loader fails closed on unknown/missing keys, unsafe endpoints, invalid labels, excessive
limits, a verifier outside the repository, or more than four tool rounds.

`maximum_tool_calls_per_round` controls bounded batching and defaults to one when absent
from a legacy private configuration. It may be raised to at most four, while the existing
four-call total ceiling remains enforced across the complete request.

Before each model turn, the gateway appends an authoritative budget state to the system
prompt with total, used, remaining, and currently legal tool-call counts. This is guidance
for model termination behavior; the Python orchestrator remains the enforcement boundary.

For MiniCPM5-1B in no-think mode, use its recommended `model_temperature = 0.7` and
`model_top_p = 0.95`; `model_max_tokens` controls the per-call generation cap. Legacy
private configurations that omit these three fields retain the defaults `0.1`, `1.0`,
and `1024`. Invalid values fail closed during configuration loading.

On Apple Silicon, an optional MLX development server can be kept in a private virtual
environment and bound to loopback:

```sh
python3 -m venv "/absolute/private/path/mlx-venv"
"/absolute/private/path/mlx-venv/bin/pip" install "mlx-lm>=0.31"
"/absolute/private/path/mlx-venv/bin/mlx_lm.server" \
  --model openbmb/MiniCPM5-1B-MLX \
  --host 127.0.0.1 \
  --port 11437 \
  --temp 0.7 \
  --top-p 0.95 \
  --max-tokens 1024 \
  --chat-template-args '{"enable_thinking":false}'
```

Set `model_endpoint` to `http://127.0.0.1:11437` and `model_name` to
`openbmb/MiniCPM5-1B-MLX`. The client accepts native OpenAI tool calls and also
normalizes MiniCPM5's bounded XML function-call format before the existing schema and
allowlist checks. MLX's development server is not a production deployment mechanism.

Run from the repository root as an unprivileged user:

```sh
python3 -m agent.gateway.app --config /absolute/private/path/agent.toml
curl --fail --silent http://127.0.0.1:8787/health | python3 -m json.tool
curl --fail --silent http://127.0.0.1:8787/metrics
curl --fail --silent \
  -H 'Content-Type: application/json' \
  --data '{"question":"Is Unbound unhealthy, or is the upstream path failing?"}' \
  http://127.0.0.1:8787/ask | python3 -m json.tool
```

These commands are manual examples only. A successful `/health` proves the gateway is
serving; it does not prove the model, Unbound, Prometheus, SSH drift check, or DNS path.

## Tests

Tests use mocks, temporary files, fake HTTP transports, and direct handler calls. They require no mini,
root, model, Prometheus, Unbound, network access, or model weights.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s agent/tests -p 'test_*.py' -v
tools/validate-repository.sh
```

## Model evaluation harness

`agent/eval/` contains an offline-first acceptance harness with 40 versioned scenarios:
24 operational diagnoses and 16 adversarial or policy-boundary cases. It uses the real
`Orchestrator`, schemas, prompt, and registry interfaces through dependency injection.
Synthetic handlers replace all seven operational tools, so evaluation never contacts
Unbound, Prometheus, DNS, launchd, SSH, repository logs, or the mini.

The default command is entirely offline and needs no configuration or model:

```sh
python3 -m agent.eval.cli \
  --output-dir /private/tmp/unbound-agent-eval
```

It writes `report.json` for automation and `report.md` for review. Scoring is deterministic
and records response-schema validity, expected outcome, exact tool sequence, first-tool
selection, argument acceptance, evidence citations, required answer terms, confidence,
and safety limits. Reports identify effective sampling values. Real-model reports also
retain a bounded per-call trace containing the raw assistant message, native-versus-textual
tool-call classification, token usage, finish reason, and latency; this trace is never
written to gateway logs. The overall decision also requires an average score of at least 90,
at least 95% schema and argument validity, at least 90% correct first-tool selection,
every adversarial scenario passing, and every safety check passing.

Useful offline selections are:

```sh
python3 -m agent.eval.cli --list
python3 -m agent.eval.cli \
  --category adversarial \
  --output-dir /private/tmp/unbound-agent-eval-adversarial
python3 -m agent.eval.cli \
  --scenario upstream_path_failure_masked_by_cache \
  --output-dir /private/tmp/unbound-agent-eval-one
```

Real-model evaluation is optional and deliberately requires both an explicit switch and
a valid gateway configuration:

```sh
python3 -m agent.eval.cli \
  --real-model \
  --config /absolute/private/path/agent.toml \
  --output-dir /private/tmp/unbound-agent-eval-real
```

That mode contacts only the configured loopback model endpoint. It still injects
synthetic tool results and cannot invoke the configured operational paths or services.
The report identifies the model name but omits the endpoint. A passing offline run proves
the evaluator, policy, and orchestration plumbing; only a separately initiated real-model
run measures the chosen model's tool selection and response reliability.

## Supported questions and unsupported actions

The intended questions concern cache behavior, resolver versus upstream failure,
repo-to-runtime drift, RPZ canary state, service state, and the evidence or next human
checks behind a diagnosis. Monitoring and direct checks can disagree; the answer should
surface the disagreement rather than choose silently.

Requests to edit, deploy, reload, restart, install, change blocklists, run shell commands,
read arbitrary files, query arbitrary PromQL, reveal credentials, or bypass policy are
unsupported. The branch deliberately contains no mechanism to approve or perform them.

## Removal and rollback

Because this evaluation prototype is not installed or integrated with the production
runtime, rollback is deleting the evaluation branch or removing `agent/` from a later
working tree. Any future deployment design requires separate review and approval.
