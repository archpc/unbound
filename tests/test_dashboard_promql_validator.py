#!/usr/bin/env python3
"""Offline contracts for the explicitly live dashboard PromQL validator."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "dashboard_promql_validator",
    ROOT / "tools" / "validate-dashboard-promql.py",
)
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class DashboardPromQLValidatorTests(unittest.TestCase):
    def test_rate_interval_expansion_matches_fifteen_second_scrape(self):
        expression = "rate(example_total[$__rate_interval])"
        self.assertEqual(
            validator.expand_grafana_macros(expression),
            "rate(example_total[60s])",
        )
        expected = {
            15: 60,
            30: 60,
            60: 75,
            300: 315,
            1800: 1815,
            7200: 7215,
        }
        for step, seconds in expected.items():
            with self.subTest(step=step):
                self.assertEqual(
                    validator.expand_grafana_macros(expression, step),
                    f"rate(example_total[{seconds}s])",
                )

    def test_all_dashboard_targets_and_rules_are_discovered(self):
        dashboards = validator.dashboard_queries(
            ROOT / "config" / "grafana" / "dashboards"
        )
        rules = validator.rule_queries(
            ROOT / "config" / "prometheus" / "prometheus.rules.yml"
        )
        self.assertEqual(len(dashboards), 101)
        self.assertEqual(len(rules), 50)
        identities = [item["identity"] for item in dashboards + rules]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertTrue(all("${client:regex}" not in item["expression"] for item in dashboards))

    def test_required_deployed_checks_are_named_without_sensitive_values(self):
        names = [name for name, _ in validator.REQUIRED_DEPLOYED_QUERIES]
        expressions = [expression for _, expression in validator.REQUIRED_DEPLOYED_QUERIES]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("monitoring:target_inventory_ok", expressions)
        self.assertIn('probe_success{job="blackbox_grafana"}', expressions)
        self.assertIn("time_machine_exporter_collection_success", expressions)
        self.assertTrue(all("client=" not in expression for expression in expressions))

    def test_shared_path_and_probe_alert_expressions_are_failure_domain_aware(self):
        queries = validator.rule_queries(
            ROOT / "config" / "prometheus" / "prometheus.rules.yml"
        )
        rules = {item["identity"]: item["expression"] for item in queries}
        node_gated = {
            "BlocklistUpdateStale",
            "BlocklistBreadcrumbAbsent",
            "WarmlistMineStale",
            "WarmlistBreadcrumbAbsent",
            "RootHintsRefreshStale",
            "RootHintsBreadcrumbAbsent",
            "WarmbootDidNotRun",
            "WarmbootBreadcrumbAbsent",
            "BlocklistMinimumMetricAbsent",
            "ContentCacheExporterStale",
            "ContentCacheTextfileMissing",
            "AlertNotifierStale",
            "AlertNotifierBreadcrumbAbsent",
            "ClientStatsStale",
            "ClientStatsBreadcrumbAbsent",
            "TimeMachineMonitoringUnhealthy",
        }
        for alert in node_gated:
            with self.subTest(alert=alert):
                self.assertIn(
                    'up{job="node"} == 1',
                    rules[f"rule:alert:{alert}"],
                )
        self.assertIn(
            'count(probe_success{job="blackbox_upstream"}) == 2',
            rules["rule:alert:UpstreamDNSPathDown"],
        )
        self.assertIn(
            "absent(unbound_up)",
            rules["rule:alert:UnboundDown"],
        )
        self.assertIn(
            'up{job="prometheus"} == 0',
            rules["rule:alert:PrometheusSelfScrapeDown"],
        )


if __name__ == "__main__":
    unittest.main()
