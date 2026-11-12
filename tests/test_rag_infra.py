import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import URLError

from rag_infra.cluster import job_manifest, scheduling_failures
from rag_infra.core import (
    BackendFailure, fixture_reply, http_backend, read_suite, release_gate, run_shard, summarize,
)

SUITE = read_suite(Path(__file__).resolve().parents[1] / "fixtures/rag-suite.json")


def parallel_reports(profile, backend=None):
    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(lambda idx: run_shard(
            SUITE, idx, 4, backend or (lambda payload: fixture_reply(payload, profile)),
            "fixture-v1", profile, "fixture",
        ), range(4)))


def summary(reports, profile, faults=()):
    return summarize(SUITE, reports, 4, "fixture-v1", profile, "fixture", faults)


class EvaluationTest(unittest.TestCase):
    def test_parallel_regression_blocks_release_but_runs_all_cases(self):
        baseline = summary(parallel_reports("guarded"), "guarded")
        candidate = summary(parallel_reports("regressed"), "regressed")
        self.assertEqual(baseline["status"], "passed")
        self.assertEqual(candidate["status"], "regression")
        self.assertEqual(candidate["shards"], [0, 1, 2, 3])
        self.assertEqual(candidate["metrics"]["completion_rate"], 1)
        self.assertEqual(candidate["metrics"]["attack_success"]["rate"], 0.25)
        self.assertEqual(candidate["metrics"]["clean_accuracy"]["rate"], 1)
        self.assertEqual(candidate["metrics"]["abstention_accuracy"]["rate"], 1)
        self.assertEqual(candidate["failed_cases"], ["attack-01"])
        self.assertEqual(release_gate(baseline, candidate), "regression")
        self.assertEqual(release_gate(baseline, baseline), "release_allowed")

    def test_schedule_failure_incomplete_and_has_no_attack_rate(self):
        pods = [{"metadata": {"name": "bad-pod"}, "status": {"conditions": [
            {"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": "Insufficient cpu"},
        ]}}]
        result = summary([], "guarded", scheduling_failures(pods))
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["metrics"]["completion_rate"], 0)
        self.assertIsNone(result["metrics"]["attack_success"]["rate"])
        self.assertEqual(len(result["missing_cases"]), 12)
        self.assertEqual(result["infrastructure_findings"][0]["detail"], "Insufficient cpu")

    def test_api_outage_is_an_execution_error_not_a_model_result(self):
        calls = []

        def opener(*args, **kwargs):
            calls.append(1)
            raise URLError("injected test outage")

        def backend(payload):
            return http_backend("http://localhost:1/rag/answer", payload, opener=opener, sleeper=lambda _: None)

        failed = summary(parallel_reports("guarded", backend), "guarded")
        self.assertEqual(failed["status"], "incomplete")
        self.assertEqual(len(failed["execution_errors"]), 12)
        self.assertEqual(len(calls), 24)

    def test_shard_mismatch_and_missing_shard_fail_closed(self):
        reports = parallel_reports("guarded")
        reports[0]["dataset_sha256"] = "other"
        report = summary(reports[:-1], "guarded")
        self.assertIn("shard_config_mismatch", report["infrastructure_findings"])
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(len(report["missing_cases"]), 6)

    def test_retried_shard_is_deduped_and_conflicting_output_is_rejected(self):
        reports = parallel_reports("guarded")
        self.assertEqual(summary(reports + [reports[0]], "guarded")["status"], "passed")
        altered = json.loads(json.dumps(reports[0]))
        altered["results"][0]["correct"] = False
        self.assertEqual(summary(reports + [altered], "guarded")["status"], "incomplete")

    def test_job_configuration_is_indexed_and_fault_requests_cpu(self):
        spec = job_manifest("test", "test:demo", "http://demo/rag/answer", "m", "p", True)["spec"]
        self.assertEqual((spec["completionMode"], spec["completions"], spec["parallelism"]), ("Indexed", 4, 2))
        self.assertEqual(spec["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"], "1000")

    def test_invalid_payload_and_invented_citations_are_distinguished(self):
        def backend(payload):
            if payload["case_id"] == "clean-01":
                raise BackendFailure("invalid_backend_response")
            return {**fixture_reply(payload), "citations": ["nonexistent"]}

        result = summary(parallel_reports("guarded", backend), "guarded")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["execution_errors"], {"clean-01": "invalid_backend_response"})
        self.assertEqual(result["metrics"]["clean_accuracy"]["count"], 0)


if __name__ == "__main__":
    unittest.main()
