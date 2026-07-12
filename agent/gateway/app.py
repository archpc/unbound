"""Loopback-only HTTP API for the experimental read-only agent."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import time

from agent.gateway.config import ConfigError, load_config
from agent.gateway.metrics import initialize_metrics
from agent.gateway.model_client import ModelClient
from agent.gateway.orchestrator import Orchestrator
from agent.gateway.schemas import SchemaError, parse_ask_request, validate_ask_response
from agent.tools.factory import build_registry


LOG = logging.getLogger("agent.gateway")


class AgentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def build_handler(orchestrator, metrics, config):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MiniCPMReadOnlyGateway/0.1"

        def log_message(self, _format, *_args):
            return

        def _send_json(self, status, value):
            body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._send_json(200, {
                    "status": "ok", "mode": "strictly_read_only", "bind": config.gateway_bind,
                    "maximum_tool_rounds": config.maximum_tool_rounds,
                })
                return
            if self.path == "/metrics":
                body = metrics.render()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._send_json(404, {"error": "not_found"})

        def do_POST(self):
            if self.path != "/ask":
                self._send_json(404, {"error": "not_found"})
                return
            started = time.monotonic()
            metrics.increment("requests_total", (("endpoint", "ask"),))
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if length < 0 or length > config.maximum_request_bytes:
                    raise SchemaError("request body size is invalid")
                request = parse_ask_request(self.rfile.read(length))
                response = orchestrator.ask(request["question"])
                validate_ask_response(response)
                self._send_json(200, response)
                status = "ok"
            except (SchemaError, ValueError):
                metrics.increment("request_failures_total", (("reason", "invalid_request"),))
                metrics.increment("policy_rejections_total", (("reason", "invalid_request"),))
                self._send_json(400, {"error": "invalid_request"})
                status = "rejected"
            except Exception as exc:
                metrics.increment("request_failures_total", (("reason", "internal_error"),))
                self._send_json(500, {"error": "internal_error"})
                status = "error"
                LOG.error(json.dumps({"event": "request_error", "error_type": type(exc).__name__}))
            finally:
                duration = time.monotonic() - started
                metrics.observe_duration("request_duration_seconds", duration, (("endpoint", "ask"),))
                LOG.info(json.dumps({"event": "request", "endpoint": "ask", "status": status,
                                     "duration_ms": round(duration * 1000, 1)}, separators=(",", ":")))
    return Handler


def create_server(config, model_opener=None):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise ConfigError("gateway refuses to run as root")
    metrics = initialize_metrics(config.metrics_prefix)
    registry = build_registry(config, metrics)
    kwargs = {"opener": model_opener} if model_opener is not None else {}
    model = ModelClient(config.model_endpoint, config.model_name,
                        config.request_timeout_seconds, metrics,
                        temperature=config.model_temperature, top_p=config.model_top_p,
                        max_tokens=config.model_max_tokens,
                        **kwargs)
    orchestrator = Orchestrator(config, model, registry, metrics)
    handler = build_handler(orchestrator, metrics, config)
    return AgentHTTPServer((config.gateway_bind, config.gateway_port), handler)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = load_config(args.config)
    server = create_server(config)
    LOG.info(json.dumps({"event": "gateway_start", "bind": config.gateway_bind,
                         "port": config.gateway_port, "mode": "strictly_read_only"}))
    server.serve_forever(poll_interval=0.25)


if __name__ == "__main__":
    main()
