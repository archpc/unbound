"""Small dependency-free Prometheus metrics registry."""

from __future__ import annotations

from collections import defaultdict
import threading


class Metrics:
    def __init__(self, prefix="agent"):
        self.prefix = prefix
        self._counters = defaultdict(float)
        self._durations = defaultdict(float)
        self._lock = threading.Lock()

    def increment(self, name, labels=(), value=1.0):
        with self._lock:
            self._counters[(name, tuple(labels))] += value

    def observe_duration(self, name, seconds, labels=()):
        key = (name, tuple(labels))
        with self._lock:
            self._counters[(name + "_count", tuple(labels))] += 1
            self._durations[(name + "_sum", tuple(labels))] += max(0.0, seconds)

    @staticmethod
    def _format_labels(labels):
        if not labels:
            return ""
        return "{" + ",".join('{}="{}"'.format(k, str(v).replace('"', '\\"')) for k, v in labels) + "}"

    def render(self):
        with self._lock:
            counters = dict(self._counters)
            durations = dict(self._durations)
        lines = []
        for (name, labels), value in sorted({**counters, **durations}.items()):
            metric = "{}_{}".format(self.prefix, name)
            lines.append("{}{} {}".format(metric, self._format_labels(labels), value))
        return ("\n".join(lines) + "\n").encode("utf-8")


REQUIRED_METRICS = (
    "requests_total", "request_duration_seconds", "request_failures_total",
    "tool_calls_total", "tool_call_duration_seconds", "tool_failures_total",
    "tool_output_bytes", "model_requests_total", "model_request_duration_seconds",
    "model_failures_total", "policy_rejections_total",
)


def initialize_metrics(prefix="agent"):
    metrics = Metrics(prefix)
    for name in REQUIRED_METRICS:
        if name.endswith("duration_seconds"):
            metrics.observe_duration(name, 0.0)
        else:
            metrics.increment(name, value=0.0)
    return metrics
