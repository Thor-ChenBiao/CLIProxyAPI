import os
import ssl
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import health_agent


class TLSCertificateHealthTests(unittest.TestCase):
    def certificate(self, not_after="Aug 24 02:48:34 2026 GMT"):
        return {
            "notAfter": not_after,
            "subjectAltName": (("DNS", "token.zasdas.com"),),
        }

    def test_certificate_with_enough_validity_is_healthy(self):
        expiry = ssl.cert_time_to_seconds("Aug 24 02:48:34 2026 GMT")
        with patch.object(health_agent, "_decode_certificate", return_value=self.certificate()):
            healthy, reason, checks = health_agent._evaluate_tls_certificate(
                now=expiry - 8 * 86400
            )

        self.assertTrue(healthy)
        self.assertEqual(reason, "ok")
        self.assertEqual(checks["tls_certificate_days_remaining"], 8.0)
        self.assertEqual(checks["tls_certificate_not_after"], "2026-08-24T02:48:34Z")

    def test_certificate_near_expiry_is_unhealthy(self):
        expiry = ssl.cert_time_to_seconds("Aug 24 02:48:34 2026 GMT")
        with patch.object(health_agent, "_decode_certificate", return_value=self.certificate()):
            healthy, reason, checks = health_agent._evaluate_tls_certificate(
                now=expiry - 6 * 86400
            )

        self.assertFalse(healthy)
        self.assertEqual(reason, "TLS certificate expires too soon")
        self.assertEqual(checks["tls_certificate_days_remaining"], 6.0)

    def test_expired_certificate_is_unhealthy(self):
        expiry = ssl.cert_time_to_seconds("Aug 24 02:48:34 2026 GMT")
        with patch.object(health_agent, "_decode_certificate", return_value=self.certificate()):
            healthy, reason, checks = health_agent._evaluate_tls_certificate(now=expiry + 1)

        self.assertFalse(healthy)
        self.assertEqual(reason, "TLS certificate expired")
        self.assertLess(checks["tls_certificate_seconds_remaining"], 0)

    def test_wrong_hostname_is_unhealthy(self):
        certificate = {
            "notAfter": "Aug 24 02:48:34 2026 GMT",
            "subjectAltName": (("DNS", "example.com"),),
        }
        with patch.object(health_agent, "_decode_certificate", return_value=certificate):
            healthy, reason, checks = health_agent._evaluate_tls_certificate(now=0)

        self.assertFalse(healthy)
        self.assertEqual(reason, "TLS certificate unavailable")
        self.assertIn("tls_certificate_error", checks)


if __name__ == "__main__":
    unittest.main()
