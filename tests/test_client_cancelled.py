# -*- coding: utf-8 -*-
"""Client cancel: ClientCancelled stops further tool-loop work."""
import unittest
from unittest import mock

from core.tool_loop import ClientCancelled, run


class ClientCancelledTests(unittest.TestCase):
    def test_exception_type(self):
        self.assertTrue(issubclass(ClientCancelled, Exception))

    def test_cancel_check_stops_loop_without_final_answer(self):
        """If the client is gone, _cancel_check / OpenRouter start must not yield a success answer."""
        state = {"phase": "pre"}

        def on_event(ev):
            t = ev.get("type")
            if t == "_cancel_check" or (
                t == "step"
                and ev.get("action") == "OpenRouter"
                and ev.get("status") == "start"
            ):
                raise ClientCancelled("socket closed")
            if t == "answer":
                state["answer"] = ev.get("text")

        with mock.patch("core.tool_loop._is_self_id_question", return_value=False):
            with mock.patch("core.tool_loop._call_llm") as call_llm:
                call_llm.return_value = "should-not-matter"
                # Skip heavy prechecks that call web/vault
                with mock.patch("core.tool_loop._run_web_precheck", return_value={}):
                    with mock.patch(
                        "core.retrieval.run_precheck",
                        create=True,
                        return_value={},
                    ):
                        try:
                            result = run(
                                "Was ist Glyph?",
                                on_event=on_event,
                                vault_search=False,
                            )
                        except ClientCancelled:
                            # Propagated to HTTP layer — also correct
                            call_llm.assert_not_called()
                            return

        self.assertTrue(result.get("cancelled") or not result.get("ok"))
        self.assertEqual((result.get("answer") or ""), "")
        self.assertNotIn("answer", state)
