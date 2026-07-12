# Threat model

The MiniCPM-class model is untrusted. A system prompt is guidance, not a security
boundary. The gateway must remain safe if the model, user input, logs, documentation,
metrics, DNS data, or a local HTTP response is malicious.

| Risk | Code-level mitigation | Residual risk |
|---|---|---|
| Prompt injection in logs or docs | Tool envelopes label content `UNTRUSTED_EVIDENCE_NEVER_INSTRUCTIONS`; fixed tool schemas are enforced after model output; evidence cannot create tools. | A model may produce a poor answer, so humans must verify important conclusions. |
| Malicious or malformed model output | Native tool calls and bounded MiniCPM5 XML calls are normalized at the client boundary; tool-call shape, arguments, final JSON, and cited tool names are then validated. Unknown or malformed XML fails closed, and malformed output becomes a stable low-confidence response. | Semantic truth cannot be proven by schema validation alone. |
| Hallucinated diagnosis or misleading confidence | Prompt requires fact/hypothesis separation; final evidence may cite only tools actually run; stable confidence enum and limitations are mandatory. | Evidence summaries remain model-authored and require operator judgment. |
| Excessive tool calls | Per-round batching is explicitly configured from one to four, defaults to one, and the orchestrator still enforces at most four total tool executions per request. Every call in a batch is validated before any handler runs. Each model turn also receives gateway-authored remaining-budget guidance. | Four expensive tools can still consume the full request deadline; prompt guidance does not guarantee model compliance. |
| Sensitive data leakage | No secret paths are configured; logs redact IPv4 and secret-like assignments; tool output is size-bounded; logs record metadata, not content. | Unrecognized sensitive formats in an approved log could evade redaction. Keep the allowlist narrow. |
| Unsafe PromQL | The caller supplies a named query ID. Expressions are immutable code constants and use an approved metric set. | Adding a query requires code review. |
| Arbitrary file access | Log names map to configured absolute paths; reads use `O_NOFOLLOW`; docs use a fixed filename list under the configured repository root. | A privileged local actor can replace regular files; local host integrity remains assumed. |
| Command injection | All subprocess calls use fixed argv arrays, no shell, no user-selected executable, and schema-validated arguments. | Bugs in invoked system binaries remain outside this prototype. |
| Denial of service | Request/body/model/tool output limits, single model concurrency, per-tool timeouts, request deadline, and bounded reads reduce cost. | Python threads cannot forcibly stop arbitrary stuck Python code; shipped tools use their own bounded I/O. |
| Excessive model memory | Model context is documented and configurable; example server uses one request slot; request and evidence sizes are bounded. | llama.cpp/model selection determines actual memory use and must be tested manually. |
| Local endpoint exposure | Config rejects non-loopback gateway, model, and Prometheus endpoints; examples bind `127.0.0.1`. | Other local users may reach loopback unless host-level controls are added. No authentication is included in this first prototype. |
| Accidental privilege escalation | Gateway refuses effective UID 0; plists specify `localadmin`; no `sudo`, write tools, root helper, or arbitrary SSH tool exists. | `localadmin` is still an admin account; a future dedicated non-admin user would reduce exposure. |
| Stale metrics | Prometheus timestamps are retained in normalized samples; answers must state limitations. | Instant queries do not by themselves establish freshness of every exporter. |
| Monitoring disagrees with direct state | Prometheus, launchd, DNS probes, and drift are separate tools; the prompt requires disagreement to be reported. | The small model may not choose all relevant corroborating tools within four rounds. |
| Unavailable control socket or services | Tools return structured unavailable results without retrying with privilege or alternate endpoints. | Diagnosis may remain incomplete. |
| Future write capability | No write tool exists. Any future proposal requires a separate branch, threat review, and explicit human approval outside this system. | None of this branch should be treated as approval for writes. |
| Evaluation accidentally reaches live state | Offline mode uses a scripted model and synthetic handlers. Real-model mode requires `--real-model --config` and still replaces every operational handler; only the validated loopback model endpoint is contacted. | A locally modified evaluator could remove the fixture boundary and requires code review. |

The gateway does not protect against a fully compromised Mac mini or an operator who
manually changes the code/configuration. It is a containment layer around an untrusted
model, not a general host sandbox.
