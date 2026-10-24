# -*- coding: utf-8 -*-
import unittest

from core.usage_meter import add_payload, reset, snapshot


class UsageMeterTest(unittest.TestCase):
    def setUp(self):
        reset()

    def test_sums_prompt_and_completion(self):
        n = add_payload({
            "usage": {"prompt_tokens": 1000, "completion_tokens": 250, "total_tokens": 1250},
        })
        self.assertEqual(n, 1250)
        self.assertEqual(snapshot(), {"input": 1000, "output": 250, "total": 1250})

    def test_second_call_accumulates(self):
        add_payload({"usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})
        add_payload({"usage": {"prompt_tokens": 400, "completion_tokens": 40, "total_tokens": 440}})
        self.assertEqual(snapshot()["total"], 560)

    def test_cache_tokens_are_not_added_twice(self):
        add_payload({
            "usage": {
                "prompt_tokens": 1000,
                "prompt_cache_hit_tokens": 800,
                "completion_tokens": 10,
                "total_tokens": 1010,
            },
        })
        self.assertEqual(snapshot()["total"], 1010)

    def test_missing_usage_adds_nothing(self):
        self.assertEqual(add_payload({"choices": []}), 0)
        self.assertEqual(snapshot()["total"], 0)

    def test_reset_clears(self):
        add_payload({"usage": {"total_tokens": 50}})
        reset()
        self.assertEqual(snapshot()["total"], 0)


if __name__ == "__main__":
    unittest.main()
