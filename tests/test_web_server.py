import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web import server


class WebServerHelpersTest(unittest.TestCase):
    def test_normalize_run_request_parses_keys_and_defaults(self):
        request = server.normalize_run_request({
            "base": " https://relay.example.com/ ",
            "keys": "primary=sk-primary\nsk-secondary",
            "name": "My Relay Audit!",
            "model": "",
            "sig_model": "",
            "n_samples": "5",
            "n_self_sig": "",
            "timeout": "90",
            "skip_tier4": True,
            "skip_cross_sig": False,
        })

        self.assertEqual(request["base"], "https://relay.example.com")
        self.assertEqual(request["name"], "My-Relay-Audit")
        self.assertEqual(request["keys"], [
            ("primary", "sk-primary"),
            ("key2", "sk-secondary"),
        ])
        self.assertEqual(request["model"], "gemini-3.1-pro-preview")
        self.assertEqual(request["sig_model"], "gemini-3-flash-preview")
        self.assertEqual(request["n_samples"], 5)
        self.assertEqual(request["n_self_sig"], 8)
        self.assertEqual(request["timeout"], 90)
        self.assertTrue(request["skip_tier4"])

    def test_build_audit_command_uses_argument_list_not_shell(self):
        request = server.normalize_run_request({
            "base": "https://relay.example.com",
            "keys": "primary=sk-primary\nsecondary=sk-secondary",
            "name": "relay",
            "model": "gemini-test",
            "sig_model": "gemini-sig",
            "n_samples": 3,
            "n_self_sig": 2,
            "timeout": 45,
            "skip_active": False,
            "skip_tier4": True,
            "skip_cross_sig": True,
        })

        command = server.build_audit_command(request, ROOT / "reports-web")

        self.assertIs(command[0], sys.executable)
        self.assertIn("--base", command)
        self.assertIn("https://relay.example.com", command)
        self.assertIn("primary=sk-primary", command)
        self.assertIn("secondary=sk-secondary", command)
        self.assertIn("--skip-tier4", command)
        self.assertIn("--skip-cross-sig", command)
        self.assertNotIn("--skip-active", command)

    def test_mask_secrets_redacts_key_values_in_cli_lines(self):
        line = "[*] using sk-primary and key2=sk-secondary"
        masked = server.mask_secrets(line, [
            ("primary", "sk-primary"),
            ("key2", "sk-secondary"),
        ])

        self.assertNotIn("sk-primary", masked)
        self.assertNotIn("sk-secondary", masked)
        self.assertIn("sk-...mary", masked)
        self.assertIn("sk-...dary", masked)

    def test_classify_cli_line_detects_probe_stage(self):
        event = server.classify_cli_line("  [active] countTokens ...")
        self.assertEqual(event["kind"], "stage")
        self.assertEqual(event["stage"], "count_tokens")
        self.assertEqual(event["label"], "countTokens")

        key_event = server.classify_cli_line("========== key1 ==========")
        self.assertEqual(key_event["kind"], "key")
        self.assertEqual(key_event["key"], "key1")

        done_event = server.classify_cli_line("[OK] report.md    -> reports/demo/report.md")
        self.assertEqual(done_event["kind"], "artifact")


if __name__ == "__main__":
    unittest.main()
