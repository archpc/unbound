"""Command-line entrypoint for offline and explicitly enabled real-model evaluation."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from agent.eval.harness import (EvaluationError, default_eval_config, load_scenarios,
                                run_evaluation)
from agent.eval.report import write_reports
from agent.gateway.config import ConfigError, load_config
from agent.gateway.metrics import initialize_metrics
from agent.gateway.model_client import ModelClient


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", default=str(Path(__file__).with_name("scenarios.json")))
    parser.add_argument("--output-dir", default="/tmp/unbound-agent-eval")
    parser.add_argument("--category", choices=("operational", "adversarial"))
    parser.add_argument("--scenario", action="append", dest="scenario_ids")
    parser.add_argument("--list", action="store_true", help="list selected scenarios and exit")
    parser.add_argument("--real-model", action="store_true",
                        help="explicitly contact the configured loopback model endpoint")
    parser.add_argument("--config", help="gateway config; required with --real-model")
    return parser.parse_args(argv)


def select_scenarios(scenarios, category=None, scenario_ids=None):
    selected = list(scenarios)
    if category:
        selected = [scenario for scenario in selected if scenario["category"] == category]
    if scenario_ids:
        requested = set(scenario_ids)
        selected = [scenario for scenario in selected if scenario["id"] in requested]
        missing = requested - {scenario["id"] for scenario in selected}
        if missing:
            raise EvaluationError("unknown scenario ids: {}".format(", ".join(sorted(missing))))
    if not selected:
        raise EvaluationError("scenario selection is empty")
    return selected


def main(argv=None):
    args = parse_args(argv)
    try:
        scenarios = select_scenarios(load_scenarios(args.scenarios), args.category, args.scenario_ids)
        if args.list:
            for scenario in scenarios:
                print("{}\t{}\t{}".format(scenario["id"], scenario["category"], scenario["question"]))
            return 0
        if args.real_model:
            if not args.config:
                raise EvaluationError("--real-model requires --config")
            config = load_config(args.config)
            metrics = initialize_metrics("agent_eval_model")
            model = ModelClient(config.model_endpoint, config.model_name,
                                config.request_timeout_seconds, metrics,
                                temperature=config.model_temperature,
                                top_p=config.model_top_p,
                                max_tokens=config.model_max_tokens)
            report = run_evaluation(scenarios, config, mode="real-model", model=model)
        else:
            if args.config:
                raise EvaluationError("--config is accepted only with --real-model")
            config = default_eval_config(REPOSITORY_ROOT)
            report = run_evaluation(scenarios, config, mode="offline")
        json_path, markdown_path = write_reports(report, args.output_dir)
        print("decision={} scenarios={} average_score={:.2f}".format(
            report["summary"]["decision"], report["summary"]["scenario_count"],
            report["summary"]["average_score"],
        ))
        print("json={}".format(json_path))
        print("markdown={}".format(markdown_path))
        return 0 if report["summary"]["decision"] == "pass" else 1
    except (EvaluationError, ConfigError, OSError) as exc:
        print("evaluation error: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
