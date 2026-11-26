"""No test reaches the network.

settings/config.py's load_dotenv() puts the developer's GROQ_API_KEY into
every test run, and the helpers that build their own client from it (the
proactive nudge reword, the reminder composer, the health check) sent 22
requests a full run to api.groq.com on the owner's free-tier quota. A run
also fetched google.com (pywhatkit's import), the weather, Hugging Face and
edge-tts. tests/_phase4_support.py now refuses to look up any host but this
machine and fails the test that tried; these tests keep that guard working.
Its stderr line is captured here, so a clean run's log names no host.

Run: python -m unittest discover -s tests -p "test_no_real_network.py"
"""
import io
import socket
import unittest
from unittest import mock

import _phase4_support as S


class NoRealNetworkTests(unittest.TestCase):
    def _forget_since(self, count):
        """These lookups are made on purpose: drop them so this test passes."""
        made = S.REAL_NETWORK_REQUESTS[count:]
        del S.REAL_NETWORK_REQUESTS[count:]
        return made

    def test_no_outside_host_resolves_and_the_log_says_so(self):
        for host in ("api.groq.com", "API.GROQ.COM.", b"huggingface.co", "speech.platform.bing.com",
                     "wttr.in", "8.8.8.8", "192.168.1.10"):
            with self.subTest(host=host):
                before = len(S.REAL_NETWORK_REQUESTS)
                with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                    with self.assertRaises(socket.gaierror) as cm:
                        socket.getaddrinfo(host, 443)
                made = self._forget_since(before)
                self.assertIn("may not reach the network", str(cm.exception))
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
        before = len(S.REAL_NETWORK_REQUESTS)
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            Reword("test_reword").run(result)
        self._forget_since(before)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1)
        report = result.failures[0][1]
        self.assertIn("api.groq.com", report)
        self.assertIn("in test_reword", report)          # the Wave/test part of the stack

    def test_this_machine_still_resolves(self):
        # FakeOpenAI and every other local test server live on 127.0.0.1.
        self.assertTrue(socket.getaddrinfo("127.0.0.1", 80))
        for host in ("localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0", "", None, socket.gethostname()):
            self.assertIsNone(S._network_host(host), host)
        self.assertEqual(S._network_host("Api.Groq.Com."), "api.groq.com")

    def test_a_stray_edge_tts_synthesis_fails_like_an_offline_pc(self):
        try:
            import edge_tts
        except ImportError:
            self.skipTest("edge-tts isn't installed")
        with self.assertRaises(ConnectionError):
            edge_tts.Communicate("hello", "en-US-AriaNeural")

    def test_hugging_face_models_come_from_the_local_cache_only(self):
        try:
            from huggingface_hub import constants
        except ImportError:
            self.skipTest("huggingface_hub isn't installed")
        self.assertTrue(constants.HF_HUB_OFFLINE)


if __name__ == "__main__":
    unittest.main()
