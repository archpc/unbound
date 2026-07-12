"""Fail-closed configuration loading for the experimental gateway."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
import re
from urllib.parse import urlparse


class ConfigError(ValueError):
    """Raised when agent configuration is unsafe or malformed."""


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class AgentConfig:
    gateway_bind: str
    gateway_port: int
    model_endpoint: str
    model_name: str
    model_context_size: int
    model_temperature: float
    model_top_p: float
    model_max_tokens: int
    request_timeout_seconds: float
    maximum_tool_rounds: int
    maximum_tool_calls_per_round: int
    per_tool_timeout_seconds: float
    maximum_tool_output_bytes: int
    maximum_request_bytes: int
    prometheus_endpoint: str
    unbound_control_path: Path
    launchctl_path: Path
    dig_path: Path
    python_path: Path
    deployment_verifier_path: Path
    repository_root: Path
    metrics_prefix: str
    approved_services: dict = field(default_factory=dict)
    approved_log_sources: dict = field(default_factory=dict)


def _parse_value(text):
    text = text.strip()
    if text in ("true", "false"):
        return text == "true"
    try:
        value = ast.literal_eval(text)
    except (SyntaxError, ValueError) as exc:
        raise ConfigError("unsupported TOML value: {}".format(text[:80])) from exc
    if not isinstance(value, (str, int, float, list)):
        raise ConfigError("unsupported TOML value type")
    return value


def parse_simple_toml(text):
    """Parse the small TOML subset used by the example without dependencies."""
    data = {}
    section = data
    for number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if not KEY_RE.fullmatch(name) or name in data:
                raise ConfigError("invalid or duplicate section on line {}".format(number))
            section = {}
            data[name] = section
            continue
        if "=" not in line:
            raise ConfigError("invalid assignment on line {}".format(number))
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not KEY_RE.fullmatch(key) or key in section:
            raise ConfigError("invalid or duplicate key on line {}".format(number))
        section[key] = _parse_value(raw_value)
    return data


def _loopback_url(value, field_name):
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS or not parsed.port:
        raise ConfigError("{} must be an explicit loopback HTTP URL with a port".format(field_name))
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError("{} must not contain credentials, query, or fragment".format(field_name))
    return value.rstrip("/")


def _absolute_path(value, field_name):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigError("{} must be absolute".format(field_name))
    return path.resolve(strict=False)


def _bounded_number(data, name, minimum, maximum, numeric_type=float):
    value = data.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("{} must be numeric".format(name))
    value = numeric_type(value)
    if not minimum <= value <= maximum:
        raise ConfigError("{} must be between {} and {}".format(name, minimum, maximum))
    return value


def load_config(path):
    config_path = Path(path)
    data = parse_simple_toml(config_path.read_text(encoding="utf-8"))
    required = {
        "gateway_bind", "gateway_port", "model_endpoint", "model_name",
        "model_context_size", "request_timeout_seconds", "maximum_tool_rounds",
        "per_tool_timeout_seconds", "maximum_tool_output_bytes", "maximum_request_bytes",
        "prometheus_endpoint", "unbound_control_path", "launchctl_path", "dig_path",
        "python_path", "deployment_verifier_path", "repository_root", "metrics_prefix",
        "approved_services", "approved_log_sources",
    }
    optional = {
        "model_temperature", "model_top_p", "model_max_tokens",
        "maximum_tool_calls_per_round",
    }
    missing = required - set(data)
    extra = set(data) - required - optional
    if missing or extra:
        raise ConfigError("configuration keys mismatch; missing={} extra={}".format(sorted(missing), sorted(extra)))
    if data["gateway_bind"] not in ("127.0.0.1", "::1"):
        raise ConfigError("gateway_bind must be a numeric loopback address")
    port = _bounded_number(data, "gateway_port", 1024, 65535, int)
    rounds = _bounded_number(data, "maximum_tool_rounds", 1, 4, int)
    repo = _absolute_path(data["repository_root"], "repository_root")
    verifier = _absolute_path(data["deployment_verifier_path"], "deployment_verifier_path")
    try:
        verifier.relative_to(repo)
    except ValueError as exc:
        raise ConfigError("deployment_verifier_path must be inside repository_root") from exc
    services = data["approved_services"]
    logs = data["approved_log_sources"]
    if not isinstance(services, dict) or not services:
        raise ConfigError("approved_services must be a non-empty table")
    if not isinstance(logs, dict) or not logs:
        raise ConfigError("approved_log_sources must be a non-empty table")
    for name, label in services.items():
        if not KEY_RE.fullmatch(name) or not isinstance(label, str) or not LABEL_RE.fullmatch(label):
            raise ConfigError("invalid approved service entry")
    safe_logs = {}
    for name, raw_path in logs.items():
        if not KEY_RE.fullmatch(name) or not isinstance(raw_path, str):
            raise ConfigError("invalid approved log entry")
        safe_path = _absolute_path(raw_path, "approved_log_sources.{}".format(name))
        lowered = safe_path.as_posix().lower()
        forbidden_markers = ("/.ssh/", "ntfy.topic", "credential", "private_key", "id_rsa", "id_ed25519", "authorized_keys")
        if any(marker in lowered for marker in forbidden_markers) or safe_path.suffix.lower() in (".key", ".pem", ".p12"):
            raise ConfigError("approved log source resembles a credential path")
        safe_logs[name] = safe_path
    prefix = data["metrics_prefix"]
    if not isinstance(prefix, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", prefix):
        raise ConfigError("metrics_prefix must be a Prometheus-safe prefix")
    model_name = data["model_name"]
    if not isinstance(model_name, str) or not 1 <= len(model_name) <= 200:
        raise ConfigError("model_name must be a non-empty string")
    return AgentConfig(
        gateway_bind=data["gateway_bind"], gateway_port=port,
        model_endpoint=_loopback_url(data["model_endpoint"], "model_endpoint"),
        model_name=model_name,
        model_context_size=_bounded_number(data, "model_context_size", 2048, 32768, int),
        model_temperature=_bounded_number(
            {"model_temperature": data.get("model_temperature", 0.1)},
            "model_temperature", 0, 2,
        ),
        model_top_p=_bounded_number(
            {"model_top_p": data.get("model_top_p", 1.0)},
            "model_top_p", 0.01, 1,
        ),
        model_max_tokens=_bounded_number(
            {"model_max_tokens": data.get("model_max_tokens", 1024)},
            "model_max_tokens", 1, 8192, int,
        ),
        request_timeout_seconds=_bounded_number(data, "request_timeout_seconds", 1, 300),
        maximum_tool_rounds=rounds,
        maximum_tool_calls_per_round=_bounded_number(
            {"maximum_tool_calls_per_round": data.get("maximum_tool_calls_per_round", 1)},
            "maximum_tool_calls_per_round", 1, 4, int,
        ),
        per_tool_timeout_seconds=_bounded_number(data, "per_tool_timeout_seconds", 0.1, 60),
        maximum_tool_output_bytes=_bounded_number(data, "maximum_tool_output_bytes", 512, 1048576, int),
        maximum_request_bytes=_bounded_number(data, "maximum_request_bytes", 128, 1048576, int),
        prometheus_endpoint=_loopback_url(data["prometheus_endpoint"], "prometheus_endpoint"),
        unbound_control_path=_absolute_path(data["unbound_control_path"], "unbound_control_path"),
        launchctl_path=_absolute_path(data["launchctl_path"], "launchctl_path"),
        dig_path=_absolute_path(data["dig_path"], "dig_path"),
        python_path=_absolute_path(data["python_path"], "python_path"),
        deployment_verifier_path=verifier, repository_root=repo, metrics_prefix=prefix,
        approved_services=dict(services), approved_log_sources=safe_logs,
    )
