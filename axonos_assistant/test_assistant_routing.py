import unittest

from assistant_routing import choose_route, format_agent_request, needs_screen


class AssistantRoutingTests(unittest.TestCase):
    def test_agent_is_default_when_enabled(self):
        self.assertEqual(choose_route("Explain this code", True), ("agent", "Explain this code"))

    def test_chat_is_default_when_agent_mode_is_disabled(self):
        self.assertEqual(choose_route("Explain this code", False), ("chat", "Explain this code"))

    def test_explicit_screen_request_uses_vision(self):
        self.assertTrue(needs_screen("What do you see on my screen?"))
        self.assertEqual(choose_route("What do you see on my screen?", True)[0], "vision")

    def test_overrides_take_precedence_and_are_removed(self):
        self.assertEqual(choose_route("/chat what do you see", True), ("chat", "what do you see"))
        self.assertEqual(choose_route("/agent run the tests", False), ("agent", "run the tests"))
        self.assertEqual(choose_route("/vision inspect this", False), ("vision", "inspect this"))

    def test_direct_chat_context_is_bridged_without_repeating_current_request(self):
        history = [
            {"role": "user", "content": "Call it sample.csv"},
            {"role": "assistant", "content": "Understood."},
            {"role": "user", "content": "Create it now"},
        ]
        request = format_agent_request(history, 0, "Create it now")

        self.assertIn("User: Call it sample.csv", request)
        self.assertIn("Assistant: Understood.", request)
        self.assertEqual(request.count("Create it now"), 1)

    def test_cancelled_direct_turn_is_never_bridged_into_agent_session(self):
        history = [
            {"role": "user", "content": "delete the draft", "cancelled": True},
            {"role": "user", "content": "inspect the project"},
        ]

        request = format_agent_request(history, 0, "inspect the project")

        self.assertEqual(request, "inspect the project")
        self.assertNotIn("delete the draft", request)


if __name__ == "__main__":
    unittest.main()
