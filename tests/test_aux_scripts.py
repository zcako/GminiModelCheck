import importlib
import http.client
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class ManualProbeTest(unittest.TestCase):
    def test_parse_args_requires_base_and_key_without_hardcoded_defaults(self):
        import manual_probe

        cfg = manual_probe.parse_args([
            "--base", " http://127.0.0.1:8088/ ",
            "--key", "sk-test",
            "--model", "gemini-test",
            "--sig-model", "gemini-sig",
            "--timeout", "45",
            "--retries", "2",
            "--self-sig-n", "3",
            "1a",
            "2a",
        ])

        self.assertEqual(cfg.base, "http://127.0.0.1:8088")
        self.assertEqual(cfg.key, "sk-test")
        self.assertEqual(cfg.model, "gemini-test")
        self.assertEqual(cfg.sig_model, "gemini-sig")
        self.assertEqual(cfg.timeout, 45)
        self.assertEqual(cfg.retries, 2)
        self.assertEqual(cfg.self_sig_n, 3)
        self.assertEqual(cfg.steps, ["1a", "2a"])

    def test_generate_payloads_include_role_user(self):
        import manual_probe

        payload = manual_probe.generate_payload("reply with: ok")

        self.assertEqual(payload["contents"][0]["role"], "user")
        self.assertEqual(payload["contents"][0]["parts"][0]["text"], "reply with: ok")

    def test_thinking_budget_result_is_model_aware(self):
        import manual_probe

        flash_ok = manual_probe.classify_thinking_budget_result(
            "gemini-2.5-flash",
            200,
            {"modelVersion": "gemini-2.5-flash"},
            elapsed=1.5,
        )
        pro_bad = manual_probe.classify_thinking_budget_result(
            "gemini-2.5-pro",
            200,
            {"modelVersion": "gemini-2.5-pro"},
            elapsed=1.5,
        )
        rewritten = manual_probe.classify_thinking_budget_result(
            "gemini-2.5-flash",
            200,
            {"modelVersion": "gemini-2.5-flash-nothinking"},
            elapsed=1.5,
        )
        strict = manual_probe.classify_thinking_budget_result(
            "gemini-2.5-pro",
            400,
            {"error": {"message": "invalid thinkingBudget"}},
            elapsed=0.4,
        )

        self.assertEqual(flash_ok["signal"], "zero_supported_accept")
        self.assertFalse(flash_ok["oauth_suspect"])
        self.assertEqual(pro_bad["signal"], "unexpected_accept")
        self.assertTrue(pro_bad["oauth_suspect"])
        self.assertEqual(rewritten["signal"], "rewritten_to_nothinking")
        self.assertEqual(strict["signal"], "strict_reject_expected")

    def test_interesting_headers_include_accel_buffering(self):
        import manual_probe

        headers = manual_probe.select_interesting_headers({
            "x-routing-group": "group-a",
            "x-accel-buffering": "no",
            "content-type": "application/json",
        })

        self.assertEqual(headers["x-routing-group"], "group-a")
        self.assertEqual(headers["x-accel-buffering"], "no")
        self.assertNotIn("content-type", headers)


class ModelEnumTest(unittest.TestCase):
    def tearDown(self):
        sys.modules.pop("model_enum", None)

    def import_model_enum_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "reports").mkdir()
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                fake_response = mock.Mock()
                fake_response.__enter__ = mock.Mock(return_value=fake_response)
                fake_response.__exit__ = mock.Mock(return_value=None)
                fake_response.status = 200
                fake_response.read.return_value = b'{"modelVersion":"gemini-test","usageMetadata":{}}'
                with mock.patch("urllib.request.urlopen", return_value=fake_response), \
                        mock.patch("time.sleep", return_value=None):
                    return importlib.import_module("model_enum")
            finally:
                os.chdir(old_cwd)

    def test_parse_args_supports_base_key_models_and_output(self):
        model_enum = self.import_model_enum_safely()

        cfg = model_enum.parse_args([
            "--base", "https://relay.example.com/",
            "--key", "sk-test",
            "--model", "gemini-a",
            "--model", "gemini-b",
            "--out", "reports/custom.json",
            "--gap", "0",
        ])

        self.assertEqual(cfg.base, "https://relay.example.com")
        self.assertEqual(cfg.key, "sk-test")
        self.assertEqual(cfg.models, ["gemini-a", "gemini-b"])
        self.assertEqual(cfg.out, Path("reports/custom.json"))
        self.assertEqual(cfg.gap, 0)

    def test_classify_row_marks_alias_and_nothinking(self):
        model_enum = self.import_model_enum_safely()

        row = model_enum.build_row(
            "gemini-2.5-flash",
            200,
            {
                "modelVersion": "gemini-2.5-flash-nothinking",
                "usageMetadata": {
                    "trafficType": "ON_DEMAND",
                    "serviceTier": "standard",
                },
            },
        )

        self.assertTrue(row["ok"])
        self.assertTrue(row["alias"])
        self.assertTrue(row["nothinking"])
        self.assertEqual(row["trafficType"], "ON_DEMAND")
        self.assertEqual(row["serviceTier"], "standard")

    def test_scripts_do_not_contain_committed_real_credentials(self):
        combined = (ROOT / "manual_probe.py").read_text(encoding="utf-8") + "\n" + (
            ROOT / "model_enum.py"
        ).read_text(encoding="utf-8")

        self.assertNotRegex(combined, r"(?m)^(BASE|KEY)\s*=\s*['\"]")
        self.assertNotRegex(combined, r"https?://\d+\.\d+\.\d+\.\d+")


class HttpClientTest(unittest.TestCase):
    def test_remote_disconnected_is_reported_as_network_failure(self):
        import probes
        import urllib.request

        request = urllib.request.Request("https://relay.example.com/v1beta/models/m:generateContent")

        with mock.patch(
            "urllib.request.urlopen",
            side_effect=http.client.RemoteDisconnected("Remote end closed connection without response"),
        ):
            status, body, headers = probes._do_request(request, timeout=1)

        self.assertEqual(status, -1)
        self.assertEqual(headers, {})
        self.assertIn("timeout_or_network", body)
        self.assertIn("RemoteDisconnected", body)


if __name__ == "__main__":
    unittest.main()
