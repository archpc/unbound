"""Evidence-first bounded model/tool orchestration."""

from __future__ import annotations

import json
from pathlib import Path
import time

from agent.gateway.model_client import ModelError
from agent.gateway.policy import rejection_reason
from agent.gateway.schemas import SchemaError, degraded_response, validate_ask_response


class Orchestrator:
    def __init__(self, config, model_client, registry, metrics, prompt_path=None):
        self.config = config
        self.model_client = model_client
        self.registry = registry
        self.metrics = metrics
        path = prompt_path or Path(__file__).resolve().parents[1] / "prompts" / "system.txt"
        self.system_prompt = Path(path).read_text(encoding="utf-8")

    def _system_prompt_with_budget(self, used_count):
        remaining = max(0, self.config.maximum_tool_rounds - used_count)
        legal_batch = min(self.config.maximum_tool_calls_per_round, remaining)
        return "{}\n\n{}".format(self.system_prompt.rstrip(), "\n".join((
            "GATEWAY_TOOL_BUDGET_STATE (gateway-authoritative):",
            "- total tool-call limit: {}".format(self.config.maximum_tool_rounds),
            "- tool calls already executed: {}".format(used_count),
            "- tool calls remaining: {}".format(remaining),
            "- maximum tool calls legal in this turn: {}".format(legal_batch),
            "Do not request more than the legal count. If the available evidence is sufficient, "
            "return the required final JSON now.",
        )))

    def ask(self, question):
        reason = rejection_reason(question)
        if reason is not None:
            self.metrics.increment("policy_rejections_total", (("reason", reason),))
            return degraded_response(
                "That request is outside the strictly read-only agent policy.",
                "The gateway rejected the request before contacting the model.",
            )
        deadline = time.monotonic() + self.config.request_timeout_seconds
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": question},
        ]
        used_tools = []
        for round_index in range(self.config.maximum_tool_rounds + 1):
            if time.monotonic() >= deadline:
                return degraded_response("The request timed out before a supported diagnosis was produced.",
                                         "The request deadline was reached.")
            try:
                messages[0] = {
                    "role": "system",
                    "content": self._system_prompt_with_budget(len(used_tools)),
                }
                remaining = max(0.1, deadline - time.monotonic())
                message = self.model_client.chat(
                    messages, self.registry.model_definitions(), timeout_seconds=remaining
                )
            except ModelError as exc:
                return degraded_response("The local model is unavailable, so no diagnosis was produced.", str(exc))
            tool_calls = message.get("tool_calls", [])
            if not tool_calls:
                return self._parse_final(message.get("content", ""), used_tools)
            if round_index >= self.config.maximum_tool_rounds:
                self.metrics.increment("policy_rejections_total", (("reason", "tool_round_limit"),))
                return degraded_response("The diagnosis stopped at the read-only tool-call limit.",
                                         "The model requested more than {} tool rounds.".format(self.config.maximum_tool_rounds))
            if len(tool_calls) > self.config.maximum_tool_calls_per_round:
                self.metrics.increment("policy_rejections_total", (("reason", "multiple_tools"),))
                return degraded_response("The model requested an unsupported batch of tool calls.",
                                         "At most {} allowlisted tool calls are permitted per round.".format(
                                             self.config.maximum_tool_calls_per_round
                                         ))
            if len(used_tools) + len(tool_calls) > self.config.maximum_tool_rounds:
                self.metrics.increment("policy_rejections_total", (("reason", "tool_round_limit"),))
                return degraded_response("The diagnosis stopped at the read-only tool-call limit.",
                                         "The model requested more than {} total tool calls.".format(
                                             self.config.maximum_tool_rounds
                                         ))
            parsed_calls = []
            for call in tool_calls:
                try:
                    call_id = call["id"]
                    function = call["function"]
                    name = function["name"]
                    arguments = json.loads(function.get("arguments", "{}"))
                    if not isinstance(call_id, str) or not isinstance(name, str) or not isinstance(arguments, dict):
                        raise ValueError("malformed tool call")
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    self.metrics.increment("policy_rejections_total", (("reason", "malformed_tool_call"),))
                    return degraded_response("The model returned a malformed tool request.",
                                             "No tool was run because the request could not be validated.")
                error = self.registry.validation_error(name, arguments)
                if error is not None:
                    self.metrics.increment("policy_rejections_total", (("reason", "invalid_tool_batch"),))
                    return degraded_response("The model requested a tool or arguments outside the policy.", error)
                parsed_calls.append((call_id, name, arguments))
            messages.append(message)
            for call_id, name, arguments in parsed_calls:
                envelope = self.registry.execute(
                    name, arguments, timeout_seconds=deadline - time.monotonic()
                )
                if envelope["status"] == "rejected":
                    return degraded_response("The model requested a tool or arguments outside the policy.",
                                             envelope["data"]["error"])
                used_tools.append(name)
                messages.append({
                    "role": "tool", "tool_call_id": call_id, "name": name,
                    "content": json.dumps(envelope, sort_keys=True, separators=(",", ":")),
                })
        return degraded_response("No diagnosis was produced.", "The bounded orchestration loop ended unexpectedly.")

    @staticmethod
    def _parse_final(content, used_tools):
        try:
            value = json.loads(content)
            return validate_ask_response(value, used_tools)
        except (json.JSONDecodeError, SchemaError, TypeError):
            return degraded_response("The local model returned a malformed answer.",
                                     "The response did not match the stable /ask schema.")
