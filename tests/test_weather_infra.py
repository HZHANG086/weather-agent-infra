import io
import json
import subprocess
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from inspect_cluster import capi_expected_workers, inspect, main
from weather_agent import Handler, answer


def node(name, ready="True", control=False):
    labels = {"node-role.kubernetes.io/control-plane": ""} if control else {}
    return {"metadata": {"name": name, "labels": labels},
            "status": {"conditions": [{"type": "Ready", "status": ready}]}}


class WeatherTest(unittest.TestCase):
    def test_cites_evidence_and_rule(self):
        reply = answer("Hangzhou", "alert")
        self.assertTrue(reply["demo_alert"])
        self.assertEqual(reply["rule"]["id"], "demo-rain-10")
        self.assertEqual(reply["evidence"][0]["id"], "demo-hz-02")
        self.assertEqual(answer("beijing", "trend")["rain_change_mm"], -3)

    def test_refuses_missing_evidence(self):
        self.assertEqual(answer("unknown", "trend")["status"], "insufficient_evidence")
        self.assertEqual(answer("hangzhou", "forecast")["evidence"], [])

    def test_http_routes(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(base + "/readyz", timeout=2) as response:
                    self.assertEqual(json.load(response), {"status": "ok"})
                with urlopen(base + "/ask?city=hangzhou&intent=trend", timeout=2) as response:
                    self.assertEqual(json.load(response)["trend"], "up")
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + "/ask?city=hangzhou", timeout=2)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
            finally:
                server.shutdown()
                thread.join()


class ClusterTest(unittest.TestCase):
    @patch("inspect_cluster.kubectl_json")
    def test_cli_compares_capi_topology_to_the_workload_cluster(self, kubectl):
        kubectl.side_effect = [
            {"spec": {"topology": {"workers": {"machineDeployments": [{"replicas": 2}]}}}},
            {"items": [node("worker")]}, {"items": []}, {"items": []},
        ]
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--context", "workload", "--management-context", "management",
                                   "--capi-cluster", "weather-workload"]), 1)
        self.assertEqual(json.loads(output.getvalue())["workers"]["expected"], 2)
        self.assertEqual(kubectl.call_args_list[0].args,
                         ("management", "clusters.cluster.x-k8s.io", "default", "weather-workload"))

    def test_capi_rejects_unsupported_topology(self):
        with self.assertRaises(ValueError):
            capi_expected_workers({"spec": {"topology": {"workers": {"machineDeployments": [{}]}}}})

    def test_worker_readiness_and_recovery(self):
        nodes = [node("control", control=True), node("worker-1"), node("worker-2", "Unknown")]
        broken = inspect(nodes, [], [], 2)
        self.assertEqual(broken["workers"], {"expected": 2, "registered": 2, "ready": 1})
        self.assertEqual([f["reason"] for f in broken["findings"]], ["NodeNotReady", "WorkerCountMismatch"])
        self.assertEqual(inspect(nodes[:2] + [node("worker-2")], [], [], 2)["status"], "healthy")
        self.assertEqual(inspect(nodes[:2], [], [], 2)["findings"][0]["reason"], "WorkerCountMismatch")

    def test_pod_and_deployment_diagnostics(self):
        pod = {"metadata": {"name": "broken", "namespace": "weather"}, "status": {
            "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}],
            "initContainerStatuses": [{"name": "init", "state": {"waiting": {"reason": "ErrImagePull"}}}],
        }}
        deployment = {"metadata": {"name": "weather-agent", "generation": 2},
                      "spec": {"replicas": 2}, "status": {"availableReplicas": 1, "observedGeneration": 1}}
        findings = inspect([node("worker")], [pod], [deployment])["findings"]
        self.assertEqual({f["reason"] for f in findings},
                         {"Unschedulable", "ErrImagePull", "ReplicaShortfall", "GenerationPending"})

    @patch("inspect_cluster.subprocess.run")
    def test_cli_uses_explicit_context_and_reports_degraded(self, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, json.dumps({"items": [node("worker")]})),
            subprocess.CompletedProcess([], 0, '{"items":[]}'),
            subprocess.CompletedProcess([], 0, '{"items":[]}'),
        ]
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--context", "test", "--expected-workers", "2"]), 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "degraded")
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:3], ["kubectl", "--context", "test"])

    @patch("inspect_cluster.subprocess.run", side_effect=subprocess.TimeoutExpired("kubectl", 30))
    def test_cli_distinguishes_api_failure(self, _):
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(main(["--context", "test"]), 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "error")


if __name__ == "__main__":
    unittest.main()
