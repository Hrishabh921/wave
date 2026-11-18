"""Phase 4 / idea 2 - provider-agnostic LLM layer (brain/llm_provider.py).

Everything runs against an in-process fake OpenAI-compatible server - no
network, no real keys, no Groq SDK needed.
"""
import json
import socket
import sys
import time
import types
import unittest

import _phase4_support as S  # noqa: F401
from _fake_openai import FakeOpenAI, Reply, completion, error_body, tool_call, sse_body, sse_chunk

from brain import llm_provider as lp
from brain.llm_provider import LLMError, OpenAICompatClient, ProviderConfig, build_client, resolve_provider


def cfg_for(srv, **over):
    base = dict(name="custom", base_url=srv.url, api_key="sk-secret-1234567890", model="m1",
                vision_model="m1", timeout=5.0, max_retries=2, needs_key=True, key_var="LLM_API_KEY")
    base.update(over)
    return ProviderConfig(**base)


class ResolveTests(unittest.TestCase):
    def r(self, env, sdk=False):
        return resolve_provider(env, groq_sdk_available=sdk)

    def test_legacy_groq_key_alone_keeps_pre_phase4_behaviour(self):
        c = self.r({"GROQ_API_KEY": "gsk_realkey1234"}, sdk=True)
        self.assertTrue(c.configured)
        self.assertEqual((c.name, c.model, c.vision_model), ("groq", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"))
        self.assertTrue(c.use_sdk)
        self.assertEqual(c.key_var, "GROQ_API_KEY")

    def test_groq_without_the_sdk_uses_the_generic_client(self):
        c = self.r({"GROQ_API_KEY": "gsk_realkey1234"}, sdk=False)
        self.assertTrue(c.configured and not c.use_sdk)
        self.assertIsInstance(build_client(c), OpenAICompatClient)
        self.assertEqual(c.base_url, "https://api.groq.com/openai/v1")

    def test_missing_and_placeholder_keys_are_reported_not_sent(self):
        for env, word in (({}, "missing"), ({"GROQ_API_KEY": ""}, "missing"),
                          ({"GROQ_API_KEY": "your_groq_api_key_here"}, "placeholder")):
            with self.subTest(env=env):
                c = self.r(env)
                self.assertFalse(c.configured)
                self.assertIn("GROQ_API_KEY", c.problem)
                self.assertIn(word, c.problem)
                self.assertIsNone(build_client(c))

    def test_one_line_switch_to_each_provider(self):
        o = self.r({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-abc12345"})
        self.assertEqual((o.name, o.base_url), ("openai", "https://api.openai.com/v1"))
        self.assertTrue(o.configured and not o.use_sdk)
        self.assertEqual(o.vision_model, o.model)                     # modern models are multimodal

        r = self.r({"LLM_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "sk-or-abc12345"})
        self.assertEqual((r.name, r.base_url), ("openrouter", "https://openrouter.ai/api/v1"))
        self.assertEqual(r.headers.get("X-Title"), "Wave AI")

        l = self.r({"LLM_PROVIDER": "ollama", "OLLAMA_MODEL": "qwen3:8b"})
        self.assertTrue(l.configured)                                  # no key needed
        self.assertEqual((l.base_url, l.model), ("http://localhost:11434/v1", "qwen3:8b"))
        self.assertEqual(l.timeout, 120.0)                             # local CPU inference is slow

        l2 = self.r({"LLM_PROVIDER": "local", "OLLAMA_HOST": "192.168.1.9:11434"})   # alias, scheme-less host
        self.assertEqual((l2.name, l2.base_url), ("ollama", "http://192.168.1.9:11434/v1"))

    def test_custom_needs_a_url_and_a_bare_base_url_implies_custom(self):
        c = self.r({"LLM_PROVIDER": "custom", "LLM_MODEL": "x"})
        self.assertFalse(c.configured)
        self.assertIn("LLM_BASE_URL", c.problem)
        c = self.r({"LLM_BASE_URL": "http://localhost:1234/v1/chat/completions/", "LLM_MODEL": "local-model"})
        self.assertEqual((c.name, c.base_url), ("custom", "http://localhost:1234/v1"))
        self.assertTrue(c.configured and not c.needs_key)

    def test_llm_api_key_beats_provider_specific_var_and_model_override(self):
        c = self.r({"LLM_PROVIDER": "openai", "LLM_API_KEY": "sk-universal-1", "OPENAI_API_KEY": "sk-other-2",
                    "LLM_MODEL": "my-model", "LLM_VISION_MODEL": "my-vision"})
        self.assertEqual((c.api_key, c.key_var, c.model, c.vision_model),
                         ("sk-universal-1", "LLM_API_KEY", "my-model", "my-vision"))

    def test_bad_values_never_raise(self):
        self.assertIn("Unknown LLM_PROVIDER", self.r({"LLM_PROVIDER": "skynet"}).problem)
        self.assertIn("http", self.r({"LLM_PROVIDER": "custom", "LLM_BASE_URL": "localhost:1234", "LLM_MODEL": "m"}).problem)
        c = self.r({"GROQ_API_KEY": "gsk_realkey1234", "LLM_TIMEOUT": "abc", "LLM_MAX_RETRIES": "99",
                    "LLM_REASONING_EFFORT": "ludicrous"})
        self.assertEqual((c.timeout, c.max_retries, c.reasoning), (60.0, 5, "auto"))

    def test_describe_masks_the_key(self):
        c = self.r({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-supersecretvalue9999"})
        d = c.describe()
        self.assertNotIn("supersecret", json.dumps(d))
        self.assertTrue(d["key"].endswith("9999"))
        self.assertEqual(d["endpoint"], "api.openai.com")

    def test_sdk_is_used_only_for_stock_groq(self):
        fake = types.ModuleType("groq")

        class Groq:
            def __init__(self, api_key=None):
                self.api_key = api_key
        fake.Groq = Groq
        sys.modules["groq"] = fake
        try:
            c = resolve_provider({"GROQ_API_KEY": "gsk_realkey1234"})
            self.assertTrue(c.use_sdk)
            client = build_client(c)
            self.assertIsInstance(client, Groq)
            self.assertEqual(client.api_key, "gsk_realkey1234")
            proxied = resolve_provider({"LLM_PROVIDER": "groq", "GROQ_API_KEY": "gsk_realkey1234",
                                        "LLM_BASE_URL": "https://proxy.example/v1"})
            self.assertFalse(proxied.use_sdk)
            self.assertIsInstance(build_client(proxied), OpenAICompatClient)
        finally:
            sys.modules.pop("groq", None)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.srv = FakeOpenAI().start()
        self.addCleanup(self.srv.stop)

    def client(self, **over):
        return OpenAICompatClient(cfg_for(self.srv, **over))

    def create(self, client=None, **kw):
        client = client or self.client()
        kw.setdefault("messages", [{"role": "user", "content": "hi"}])
        return client.chat.completions.create(**kw)

    # ---------- wire format ----------
    def test_request_shape_and_auth_header(self):
        self.srv.script = [Reply(200, completion("hello there"))]
        tools = [{"type": "function", "function": {"name": "t", "description": "d",
                                                    "parameters": {"type": "object", "properties": {}, "required": []}}}]
        resp = self.create(tools=tools, tool_choice="auto", temperature=0.4, max_tokens=123,
                           extra_body={"reasoning_effort": "high"})
        self.assertEqual(resp.choices[0].message.content, "hello there")
        self.assertIsNone(resp.choices[0].message.tool_calls)
        req = self.srv.posts()[0]
        self.assertEqual(req.path, "/v1/chat/completions")
        self.assertEqual(req.headers["authorization"], "Bearer sk-secret-1234567890")
        self.assertEqual(req.json["model"], "m1")
        self.assertEqual((req.json["temperature"], req.json["max_tokens"], req.json["tool_choice"],
                          req.json["reasoning_effort"]), (0.4, 123, "auto", "high"))
        self.assertEqual(req.json["tools"], tools)

    def test_no_key_means_no_authorization_header_and_no_tools_means_no_tool_choice(self):
        self.srv.script = [Reply(200, completion("ok"))]
        self.create(self.client(api_key="", needs_key=False), tool_choice="auto")
        req = self.srv.posts()[0]
        self.assertNotIn("authorization", req.headers)
        self.assertNotIn("tools", req.json)
        self.assertNotIn("tool_choice", req.json)

    def test_reasoning_modes(self):
        for mode, expect in (("off", None), ("low", "low"), ("auto", "high")):
            with self.subTest(mode=mode):
                self.srv.requests.clear()
                self.srv.script = [Reply(200, completion("ok"))]
                self.create(self.client(reasoning=mode), extra_body={"reasoning_effort": "high"})
                self.assertEqual(self.srv.posts()[0].json.get("reasoning_effort"), expect)

    def test_tool_calls_are_normalised_to_the_sdk_shape(self):
        self.srv.script = [Reply(200, completion(None, [
            tool_call("open_app", {"app_name": "chrome"}, id="call_1"),
            {"function": {"name": "close_app", "arguments": {"app_name": "x"}}},   # no id, dict args
            {"id": "call_3", "function": {"name": "lock_pc"}},                      # no args at all
        ]))]
        msg = self.create().choices[0].message
        calls = msg.tool_calls
        self.assertEqual([c.function.name for c in calls], ["open_app", "close_app", "lock_pc"])
        self.assertEqual(json.loads(calls[0].function.arguments), {"app_name": "chrome"})
        self.assertEqual(json.loads(calls[1].function.arguments), {"app_name": "x"})
        self.assertEqual(calls[2].function.arguments, "{}")
        self.assertEqual(calls[0].id, "call_1")
        self.assertTrue(calls[1].id.startswith("call_"))
        self.assertEqual(calls[0].type, "function")

    def test_content_parts_and_empty_choices(self):
        self.srv.script = [Reply(200, {"choices": [{"message": {"content": [{"type": "text", "text": "a"},
                                                                             {"type": "text", "text": "b"}]}}]})]
        self.assertEqual(self.create().choices[0].message.content, "ab")
        self.srv.script = [Reply(200, {"choices": []})]
        with self.assertRaises(LLMError) as cm:
            self.create()
        self.assertEqual(cm.exception.kind, "bad_response")

    def test_streaming_now_returns_chunks_instead_of_being_refused(self):
        """Tier 2: stream=True used to raise LLMError unconditionally (see
        git history / CHANGELOG) - it's now a real generator of chunks.
        Deeper streaming coverage (negotiation on stream-open, mid-stream
        errors, tool-call delta accumulation) lives in
        test_phase8_llm_streaming.py; this is just the smoke test that
        belongs alongside every other ClientTests case."""
        self.srv.script = [Reply(200, sse_body(
            sse_chunk(delta={"role": "assistant"}),
            sse_chunk(delta={"content": "Hello"}),
            sse_chunk(delta={"content": " there"}),
            sse_chunk(delta={}, finish_reason="stop"),
        ))]
        chunks = list(self.create(stream=True))
        text = "".join(c.choices[0].delta.content or "" for c in chunks)
        self.assertEqual(text, "Hello there")
        self.assertEqual(chunks[-1].choices[0].finish_reason, "stop")

    # ---------- parameter negotiation ----------
    def test_reasoning_model_rejects_temperature_and_max_tokens_once_then_it_is_remembered(self):
        self.srv.script = [
            Reply(400, error_body("Unsupported parameter: 'max_tokens' is not supported with this model. "
                                  "Use 'max_completion_tokens' instead.", "max_tokens")),
            Reply(400, error_body("Unsupported value: 'temperature' does not support 0.4 with this model. "
                                  "Only the default (1) value is supported.", "temperature")),
            Reply(200, completion("worked")),
        ]
        c = self.client()
        resp = self.create(c, temperature=0.4, max_tokens=50)
        self.assertEqual(resp.choices[0].message.content, "worked")
        final = self.srv.posts()[-1].json
        self.assertNotIn("temperature", final)
        self.assertNotIn("max_tokens", final)
        self.assertEqual(final["max_completion_tokens"], 50)
        self.assertEqual(len(self.srv.posts()), 3)

        # learned: the next request is right first time
        self.srv.script = [Reply(200, completion("again"))]
        self.create(c, temperature=0.4, max_tokens=50)
        self.assertEqual(len(self.srv.posts()), 4)
        again = self.srv.posts()[-1].json
        self.assertEqual((again.get("temperature"), again["max_completion_tokens"]), (None, 50))
        self.assertEqual(c.quirks()["m1"], {"drop": ["temperature"], "max_tokens_key": "max_completion_tokens"})

    def test_non_reasoning_model_rejecting_reasoning_effort(self):
        self.srv.script = [Reply(400, error_body("Unrecognized request argument supplied: reasoning_effort")),
                           Reply(200, completion("fine"))]
        resp = self.create(extra_body={"reasoning_effort": "high"})
        self.assertEqual(resp.choices[0].message.content, "fine")
        self.assertNotIn("reasoning_effort", self.srv.posts()[-1].json)

    def test_unfixable_400_surfaces_the_server_message_and_never_loops(self):
        self.srv.default = Reply(400, error_body("Invalid 'messages[0].role': nope", "messages"))
        with self.assertRaises(LLMError) as cm:
            self.create(temperature=0.4)
        self.assertEqual(cm.exception.kind, "bad_request")
        self.assertIn("Invalid 'messages[0].role'", str(cm.exception))
        self.assertEqual(len(self.srv.posts()), 1)

    def test_a_server_that_keeps_rejecting_different_params_is_bounded(self):
        self.srv.default = Reply(400, error_body("Unsupported parameter: 'temperature' is not supported", "temperature"))
        with self.assertRaises(LLMError):
            self.create(temperature=0.4)
        self.assertLessEqual(len(self.srv.posts()), 5)

    # ---------- errors & retries ----------
    def test_auth_error_names_the_env_var_and_leaks_nothing(self):
        self.srv.script = [Reply(401, error_body("Incorrect API key provided: sk-secret-1234567890"))]
        with self.assertRaises(LLMError) as cm:
            self.create(self.client(key_var="OPENAI_API_KEY"))
        self.assertEqual(cm.exception.kind, "auth")
        self.assertIn("OPENAI_API_KEY", str(cm.exception))
        self.assertNotIn("sk-secret", str(cm.exception))
        self.assertEqual(len(self.srv.posts()), 1)   # auth errors are not retried

    def test_model_not_found(self):
        self.srv.script = [Reply(404, error_body("The model `m1` does not exist"))]
        with self.assertRaises(LLMError) as cm:
            self.create()
        self.assertEqual(cm.exception.kind, "not_found")
        self.assertIn("LLM_MODEL", str(cm.exception))

    def test_429_with_short_retry_after_is_retried(self):
        self.srv.script = [Reply(429, error_body("slow down"), {"Retry-After": "0"}), Reply(200, completion("ok"))]
        t0 = time.time()
        self.assertEqual(self.create().choices[0].message.content, "ok")
        self.assertEqual(len(self.srv.posts()), 2)
        self.assertLess(time.time() - t0, 3)

    def test_long_retry_after_fails_over_immediately_instead_of_stalling_a_voice_request(self):
        self.srv.default = Reply(429, error_body("quota"), {"Retry-After": "60"})
        t0 = time.time()
        with self.assertRaises(LLMError) as cm:
            self.create()
        self.assertEqual(cm.exception.kind, "rate_limit")
        self.assertLess(time.time() - t0, 2)
        self.assertEqual(len(self.srv.posts()), 1)

    def test_5xx_retried_then_gives_up_after_max_retries(self):
        self.srv.script = [Reply(503, error_body("overloaded")), Reply(200, completion("recovered"))]
        self.assertEqual(self.create(self.client(max_retries=2)).choices[0].message.content, "recovered")
        self.srv.requests.clear()
        self.srv.default = Reply(500, error_body("boom"))
        t0 = time.time()
        with self.assertRaises(LLMError) as cm:
            self.create(self.client(max_retries=1))
        self.assertEqual(cm.exception.kind, "server")
        self.assertEqual(len(self.srv.posts()), 2)   # 1 try + 1 retry
        self.assertLess(time.time() - t0, 4)

    def test_read_timeout_is_not_retried(self):
        self.srv.default = Reply(200, completion("too late"), delay=1.5)
        t0 = time.time()
        with self.assertRaises(LLMError) as cm:
            self.create(self.client(timeout=0.4))
        self.assertEqual(cm.exception.kind, "timeout")
        self.assertIn("LLM_TIMEOUT", str(cm.exception))
        self.assertLess(time.time() - t0, 1.4)

    def test_connection_refused_is_friendly_and_retried_once(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                                   # nothing is listening there now
        dead = OpenAICompatClient(ProviderConfig(name="ollama", base_url=f"http://127.0.0.1:{port}/v1", model="m",
                                                 timeout=2, max_retries=5, needs_key=False))
        t0 = time.time()
        with self.assertRaises(LLMError) as cm:
            dead.chat.completions.create(messages=[{"role": "user", "content": "x"}])
        self.assertEqual(cm.exception.kind, "connection")
        self.assertIn("Is the Ollama app running?", str(cm.exception))
        self.assertLess(time.time() - t0, 4)        # one 0.8s retry, not five

    def test_non_json_success_body(self):
        self.srv.script = [Reply(200, "<html>captive portal</html>")]
        with self.assertRaises(LLMError) as cm:
            self.create()
        self.assertEqual(cm.exception.kind, "bad_response")

    # ---------- health check ----------
    def test_ping_via_models_endpoint(self):
        self.srv.get_routes["/v1/models"] = Reply(200, {"data": []})
        ok, msg, ms = self.client().ping()
        self.assertTrue(ok)
        self.assertIn("Connected", msg)
        self.assertGreaterEqual(ms, 0)
        self.srv.get_routes["/v1/models"] = Reply(401, error_body("bad key"))
        ok, msg, _ = self.client().ping()
        self.assertFalse(ok)
        self.assertIn("rejected the API key", msg)

    def test_ping_falls_back_to_a_one_token_completion_when_models_is_missing(self):
        self.srv.script = [Reply(200, completion("pong"))]
        ok, msg, _ = self.client().ping()                   # GET /models -> 404 (no route)
        self.assertTrue(ok, msg)
        self.assertEqual(self.srv.posts()[0].json["max_tokens"], 1)

    def test_ping_openrouter_uses_the_key_info_endpoint(self):
        self.srv.get_routes["/v1/auth/key"] = Reply(401, error_body("No auth"))
        ok, msg, _ = self.client(name="openrouter").ping()
        self.assertFalse(ok)
        self.assertIn("/v1/auth/key", [r.path for r in self.srv.requests if r.method == "GET"])

    def test_check_connection_wrapper(self):
        ok, msg, _ = lp.check_connection(None, resolve_provider({}))
        self.assertFalse(ok)
        self.assertIn("GROQ_API_KEY", msg)


if __name__ == "__main__":
    unittest.main()
