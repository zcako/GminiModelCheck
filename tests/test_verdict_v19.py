import unittest

from probes import verdict


def raw_for(active: dict) -> dict:
    return {
        "meta": {"keys": ["k1"]},
        "per_key": {"k1": {"active": active, "tier4": {}}},
        "cross_sig_matrix": None,
    }


class VerdictV19Test(unittest.TestCase):
    def test_flash_thinking_zero_200_does_not_create_oauth_verdict(self):
        raw = raw_for({
            "thinkingBudget_zero": {
                "status": 200,
                "signal": "zero_supported_accept",
                "capability": "supports_zero",
                "oauth_suspect": False,
                "hard_oauth_evidence": False,
            },
        })

        result = verdict.compute(raw)["per_key"]["k1"]

        self.assertNotIn("OAuth", result["label"])

    def test_pro_unexpected_accept_with_routing_group_is_oauth(self):
        raw = raw_for({
            "thinkingBudget_zero": {
                "status": 200,
                "signal": "unexpected_accept",
                "capability": "requires_thinking",
                "oauth_suspect": True,
                "hard_oauth_evidence": True,
            },
            "http_headers": {
                "interesting_headers": {"x-routing-group": "gemini-cli"},
            },
        })

        result = verdict.compute(raw)["per_key"]["k1"]

        self.assertIn("OAuth", result["label"])


if __name__ == "__main__":
    unittest.main()
