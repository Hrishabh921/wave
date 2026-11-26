"""No test reaches a real LLM provider.

settings/config.py's load_dotenv() puts the developer's GROQ_API_KEY into
every test run, and the helpers that build their own client from it (the
proactive nudge reword, the reminder composer, the health check) sent 22
requests a full run to api.groq.com on the owner's free-tier quota.
tests/_phase4_support.py now refuses to look up a provider's host and fails
the test that tried; these tests keep that guard working. The guard's
stderr line is captured here, so a clean run's log never names a provider.

Run: python -m unittest discover -s tests -p "test_no_real_llm.py"
"""
import io
import socket
import unittest
from unittest import mock

import _phase4_support as S


class NoRealLlmRequestTests(unittest.TestCase):
    def _forget_since(self, count):
        """These lookups are made on purpose: drop them so this test passes."""
        made = S.REAL_LLM_REQUESTS[count:]
        del S.REAL_LLM_REQUESTS[count:]
        return made

    def test_a_provider_host_never_resolves_and_the_log_says_so(self):
        for host in ("api.groq.com", "api.openai.com", "openrouter.ai", "API.GROQ.COM.", b"api.groq.com"):
            with self.subTest(host=host):
                before = len(S.REAL_LLM_REQUESTS)
                with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                    with self.assertRaises(socket.gaierror) as cm:
                        socket.getaddrinfo(host, 443)
                made = self._forget_since(before)
                self.assertIn("real LLM provider", str(cm.exception))
                self.assertEqual(len(made), 1)
                self.assertIn(made[0][0], err.getvalue())
                self.assertIn("BLOCKED", err.getvalue())

    def test_a_test_that_tries_fails_and_names_the_host_and_the_caller(self):
        class Reword(unittest.TestCase):
            def test_reword(self):
                try:
                    socket.getaddrinfo("api.groq.com", 443)
                except OSError:
                    pass        # Wave's helpers swallow it and fall back - the test must still fail

        result = unittest.TestResult()
        before = len(S.REAL_LLM_REQUESTS)
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            Reword("test_reword").run(result)
        self._forget_since(before)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1)
        report = result.failures[0][1]
        self.assertIn("api.groq.com", report)
        self.assertIn("in test_reword", report)          # the Wave/test part of the stack

    def test_local_and_numeric_hosts_still_resolve(self):
        # FakeOpenAI and every other local test server live on 127.0.0.1.
        self.assertTrue(socket.getaddrinfo("127.0.0.1", 80))
        self.assertIsNone(S._real_llm_host("localhost"))
        self.assertIsNone(S._real_llm_host("notgroq.com"))
        self.assertEqual(S._real_llm_host("api.groq.com"), "api.groq.com")


if __name__ == "__main__":
    unittest.main()
