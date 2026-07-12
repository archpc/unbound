"""OpenAI-compatible client for an approved loopback model server."""

from __future__ import annotations

import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ModelError(RuntimeError):
    """Raised for unavailable or malformed model responses."""


FUNCTION_BLOCK_RE = re.compile(r"<function\s+name=(?:\"[^\"]+\"|'[^']+')[^>]*>.*?</function>", re.DOTALL)


def _schema_types(tools):
    definitions = {}
    for tool in tools:
        function = tool.get("function", {}) if isinstance(tool, dict) else {}
        name = function.get("name")
        parameters = function.get("parameters", {})
        properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
        if isinstance(name, str) and isinstance(properties, dict):
            definitions[name] = {
                key: value.get("type") for key, value in properties.items()
                if isinstance(key, str) and isinstance(value, dict)
            }
    return definitions


def _coerce_xml_argument(value, expected_type):
    if expected_type == "integer" and re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    if expected_type == "number":
        try:
            return float(value)
        except ValueError:
            return value
    if expected_type == "boolean" and value.lower() in ("true", "false"):
        return value.lower() == "true"
    if expected_type in ("array", "object"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        if expected_type == "array" and isinstance(parsed, list):
            return parsed
        if expected_type == "object" and isinstance(parsed, dict):
            return parsed
    return value


def normalize_minicpm_tool_calls(content, tools):
    """Convert MiniCPM5 XML calls into the OpenAI tool-call message shape."""
    if not content or "<function" not in content:
        return content, []
    definitions = _schema_types(tools)
    matches = list(FUNCTION_BLOCK_RE.finditer(content))
    if not matches:
        return content, []
    calls = []
    for index, match in enumerate(matches, 1):
        block = match.group(0)
        if "<!DOCTYPE" in block or "<!ENTITY" in block:
            return content, []
        try:
            root = ET.fromstring(block)
        except ET.ParseError:
            return content, []
        name = root.attrib.get("name")
        if root.tag != "function" or set(root.attrib) != {"name"} or name not in definitions:
            return content, []
        arguments = {}
        for child in root:
            param_name = child.attrib.get("name")
            if (child.tag != "param" or set(child.attrib) != {"name"} or not param_name
                    or len(child) or param_name in arguments):
                return content, []
            raw_value = child.text or ""
            arguments[param_name] = _coerce_xml_argument(
                raw_value, definitions[name].get(param_name)
            )
        calls.append({
            "id": "call-minicpm5-{}".format(index),
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, separators=(",", ":"))},
        })
    cleaned = FUNCTION_BLOCK_RE.sub("", content).strip()
    return cleaned, calls


class ModelClient:
    def __init__(self, endpoint, model_name, timeout_seconds, metrics, opener=urlopen,
                 temperature=0.1, top_p=1.0, max_tokens=1024):
        self.endpoint = endpoint.rstrip("/")
        self.model_name = model_name
        self.timeout_seconds = timeout_seconds
        self.metrics = metrics
        self.opener = opener
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.last_call_metadata = None
        self._single_request = threading.Lock()

    def chat(self, messages, tools, timeout_seconds=None):
        payload = {
            "model": self.model_name,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
        }
        request = Request(
            self.endpoint + "/v1/chat/completions",
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        self.metrics.increment("model_requests_total")
        try:
            with self._single_request:
                timeout = self.timeout_seconds if timeout_seconds is None else min(
                    self.timeout_seconds, max(0.1, timeout_seconds)
                )
                with self.opener(request, timeout=timeout) as response:
                    raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ModelError("model response exceeded 1 MiB")
            body = json.loads(raw.decode("utf-8"))
            choice = body["choices"][0]
            message = choice["message"]
            if not isinstance(message, dict):
                raise ModelError("model response message is not an object")
            content = message.get("content")
            tool_calls = message.get("tool_calls", [])
            if content is not None and not isinstance(content, str):
                raise ModelError("model content is not text")
            if not isinstance(tool_calls, list):
                raise ModelError("model tool_calls is not a list")
            raw_content = content or ""
            raw_tool_calls = tool_calls
            if not tool_calls:
                content, tool_calls = normalize_minicpm_tool_calls(content or "", tools)
            usage = body.get("usage", {})
            safe_usage = {}
            if isinstance(usage, dict):
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    value = usage.get(key)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        safe_usage[key] = value
            trace_limit = 32768
            self.last_call_metadata = {
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "finish_reason": choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None,
                "usage": safe_usage,
                "raw_response": {
                    "content": raw_content[:trace_limit],
                    "content_truncated": len(raw_content) > trace_limit,
                    "tool_calls": raw_tool_calls,
                },
                "classification": {
                    "native_tool_calls": bool(raw_tool_calls),
                    "minicpm_xml_in_content": "<function" in raw_content,
                    "pseudo_tool_syntax_in_content": any(marker in raw_content for marker in (
                        "<tool_call", "tool_calls", "```json", "```xml",
                    )),
                    "normalized_tool_calls": len(tool_calls) if not raw_tool_calls else 0,
                },
            }
            return {"role": "assistant", "content": content or "", "tool_calls": tool_calls}
        except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError,
                json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            self.metrics.increment("model_failures_total")
            raise ModelError("local model request failed: {}".format(type(exc).__name__)) from exc
        finally:
            self.metrics.observe_duration("model_request_duration_seconds", time.monotonic() - started)
