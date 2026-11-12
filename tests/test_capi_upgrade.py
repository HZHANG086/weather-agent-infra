import unittest

from scripts.capi_upgrade import measure


def samples(failed=()):
    return [{"seq": i, "scheduled": float(i), "started": i + .01, "ended": i + .2,
             "ok": i not in failed, "reason": "URLError" if i in failed else None}
            for i in range(23)]


class UpgradeMetricsTest(unittest.TestCase):
    def test_counts_failures_and_measures_runs_through_recovery(self):
        result = measure(samples({4, 5, 9}), 2.5, 9.5)
        self.assertEqual((result["requests"], result["failed"]), (7, 3))
        self.assertEqual(result["failure_rate_percent"], 42.857)
        self.assertEqual(result["longest_observed_interruption_seconds"], 2.19)
        self.assertEqual(result["last_request_recovery_seconds"], 1.19)
        self.assertEqual(result["two_upgraded_replicas_ready_seconds"], 7)

    def test_no_interruption_is_not_an_instant_recovery(self):
        result = measure(samples(), 2.5, 9.5)
        self.assertEqual(result["longest_observed_interruption_seconds"], 0)
        self.assertIsNone(result["last_request_recovery_seconds"])

    def test_missing_or_unhealthy_samples_do_not_claim_availability(self):
        missing = [s for s in samples() if s["seq"] != 5]
        with self.assertRaisesRegex(ValueError, "coverage"):
            measure(missing, 2.5, 9.5)
        with self.assertRaisesRegex(ValueError, "healthy"):
            measure(samples({18}), 2.5, 9.5)
