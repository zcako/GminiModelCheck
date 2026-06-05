import unittest

import model_source_probe


class ModelSourceProbeV19Test(unittest.TestCase):
    def test_count_tokens_pollution_detected(self):
        result = model_source_probe.classify_count_tokens_response(
            200,
            {
                "candidates": [{"content": {"parts": [{"text": "hello"}]}}],
                "modelVersion": "gemini-3.5-flash",
            },
        )

        self.assertEqual(result["verdict"], "endpoint_polluted")

    def test_thinking_zero_flash_is_not_oauth(self):
        result = model_source_probe.classify_thinking_zero_record(
            "gemini-3.5-flash",
            {
                "response": {
                    "status": 200,
                    "elapsed_seconds": 73.873,
                    "body": '{"modelVersion":"gemini-3.5-flash"}',
                    "headers": {},
                }
            },
        )

        self.assertIn(result["signal"], {"flash_compat_accept", "zero_supported_accept"})
        self.assertFalse(result["hard_oauth_evidence"])


if __name__ == "__main__":
    unittest.main()
