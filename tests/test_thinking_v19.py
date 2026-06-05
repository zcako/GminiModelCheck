import unittest

from probes.thinking import classify_thinking_budget_zero, model_zero_capability


class ThinkingBudgetV19Test(unittest.TestCase):
    def test_flash_zero_200_is_allowed_not_oauth(self):
        result = classify_thinking_budget_zero(
            requested_model="gemini-2.5-flash",
            status=200,
            body={"modelVersion": "gemini-2.5-flash"},
            elapsed=1.2,
        )

        self.assertEqual(result["capability"], "supports_zero")
        self.assertEqual(result["signal"], "zero_supported_accept")
        self.assertFalse(result["oauth_suspect"])
        self.assertFalse(result["hard_oauth_evidence"])

    def test_flash_lite_zero_200_is_allowed(self):
        result = classify_thinking_budget_zero(
            requested_model="gemini-2.5-flash-lite",
            status=200,
            body={"modelVersion": "gemini-2.5-flash-lite"},
            elapsed=0.9,
        )

        self.assertEqual(result["capability"], "supports_zero")
        self.assertEqual(result["signal"], "zero_supported_accept")

    def test_pro_zero_400_is_expected_strict_reject(self):
        result = classify_thinking_budget_zero(
            requested_model="gemini-2.5-pro",
            status=400,
            body={"error": {"message": "Budget 0 is invalid. This model only works in thinking mode."}},
            elapsed=0.4,
        )

        self.assertEqual(result["capability"], "requires_thinking")
        self.assertEqual(result["signal"], "strict_reject_expected")
        self.assertFalse(result["oauth_suspect"])

    def test_pro_zero_200_is_unexpected_accept(self):
        result = classify_thinking_budget_zero(
            requested_model="gemini-2.5-pro",
            status=200,
            body={"modelVersion": "gemini-2.5-pro"},
            elapsed=19.0,
        )

        self.assertEqual(result["capability"], "requires_thinking")
        self.assertEqual(result["signal"], "unexpected_accept")
        self.assertTrue(result["oauth_suspect"])
        self.assertTrue(result["latency_warning"])

    def test_nothinking_alias_is_rewrite_not_hard_oauth(self):
        result = classify_thinking_budget_zero(
            requested_model="gemini-2.5-flash",
            status=200,
            body={"modelVersion": "gemini-2.5-flash-nothinking"},
            elapsed=1.5,
        )

        self.assertEqual(result["signal"], "rewritten_to_nothinking")
        self.assertFalse(result["hard_oauth_evidence"])

    def test_model_capability_rules(self):
        self.assertEqual(model_zero_capability("gemini-2.5-flash"), "supports_zero")
        self.assertEqual(model_zero_capability("gemini-2.5-flash-lite"), "supports_zero")
        self.assertEqual(model_zero_capability("gemini-2.5-pro"), "requires_thinking")
        self.assertEqual(model_zero_capability("gemini-3-pro-image-preview"), "requires_thinking")
        self.assertEqual(model_zero_capability("gemini-3.5-flash"), "flash_compat")


if __name__ == "__main__":
    unittest.main()
