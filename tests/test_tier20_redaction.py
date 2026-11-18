"""Tier 20 item: secret redaction before anything is logged, traced, or
shown in diagnostics.

Covers:
  * brain/redaction.py       - scan_secrets()/redact() for every kind, both
                                positive (redacted) and false-positive-
                                avoidance (Luhn/Verhoeff-invalid, an
                                ordinary email) cases, plus the
                                REDACT_SECRETS_ENABLED kill-switch.
  * settings/logging_config.py - _RedactionFilter actually scrubbing a real
                                log line written to a temp file, including
                                the multi-handler-safety case (the same
                                record filtered twice, once per handler).
  * telemetry/trace.py       - annotate()/mark()/end_turn() redacting
                                string metadata before it's stored.
  * automation/health_check.py / skills/self_diagnostics.py - their report
                                strings come back redacted.

No display, no audio device, no network - uses the same
_phase4_support.install_fake_module stubbing pattern every other test file
in this suite already relies on for pyautogui/pywhatkit.
"""
import logging
import os
import tempfile
import unittest

import _phase4_support as S  # noqa: F401
from brain import redaction
from settings import logging_config as lc
from settings.config import config
from telemetry import trace


# =====================================================================
# brain/redaction.py - scan_secrets() / redact()
# =====================================================================
class RedactionKindsTests(unittest.TestCase):
    def setUp(self):
        self._enabled = config.REDACT_SECRETS_ENABLED
        config.REDACT_SECRETS_ENABLED = True

    def tearDown(self):
        config.REDACT_SECRETS_ENABLED = self._enabled

    def _assert_redacted(self, text, kind):
        out = redaction.redact(text)
        self.assertIn(f"[REDACTED:{kind}]", out)
        self.assertNotEqual(out, text)

    def test_private_key_redacted(self):
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIBOgIBAAJBAK...\n"
            "-----END RSA PRIVATE KEY-----"
        )
        self._assert_redacted(f"here is my key:\n{pem}\nthanks", "private_key")

    def test_google_api_key_redacted(self):
        self._assert_redacted("key=AIza" + "A" * 35, "google_api_key")

    def test_openai_key_redacted(self):
        self._assert_redacted("sk-" + "a" * 30, "openai_key")

    def test_github_token_redacted(self):
        self._assert_redacted("ghp_" + "a" * 36, "github_token")

    def test_aws_access_key_id_redacted(self):
        self._assert_redacted("AKIA" + "A" * 16, "aws_access_key_id")

    def test_jwt_redacted(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        self._assert_redacted(f"Authorization token: {jwt}", "jwt")

    def test_bearer_token_redacted(self):
        self._assert_redacted("Bearer abcdefghij1234567890", "bearer_token")

    def test_kv_password_redacted(self):
        self._assert_redacted("password: Sup3rSecret!", "kv_password")

    def test_groq_key_redacted(self):
        self._assert_redacted("gsk_" + "a" * 25, "groq_key")

    def test_pan_redacted(self):
        self._assert_redacted("my PAN is ABCDE1234F", "pan")

    def test_upi_id_redacted(self):
        self._assert_redacted("pay me at rahul.kumar@okaxis please", "upi_id")

    def test_ifsc_redacted(self):
        self._assert_redacted("IFSC: HDFC0001234", "ifsc")

    def test_in_phone_redacted(self):
        self._assert_redacted("call me on 9876543210 tomorrow", "in_phone")

    def test_valid_card_number_redacted(self):
        # 4532015112830366 is a genuinely Luhn-valid test number.
        self._assert_redacted("card: 4532015112830366", "card_number")

    def test_valid_aadhaar_redacted(self):
        # Brute-forced: 000000000003 is the Verhoeff-valid member of this
        # family (see test_invalid_aadhaar_not_redacted for its neighbor).
        self._assert_redacted("aadhaar 0000 0000 0003", "aadhaar")

    # -- false-positive avoidance ------------------------------------
    def test_invalid_card_number_not_redacted(self):
        text = "order number 1234567890123456"
        self.assertEqual(redaction.redact(text), text)

    def test_invalid_aadhaar_not_redacted(self):
        text = "aadhaar 0000 0000 0004"
        self.assertEqual(redaction.redact(text), text)

    def test_ordinary_email_not_flagged_as_upi(self):
        text = "reach me at rahul.kumar@gmail.com"
        self.assertEqual(redaction.redact(text), text)

    def test_plain_prose_untouched(self):
        text = "battery at 87%, turn the volume up please"
        self.assertEqual(redaction.redact(text), text)

    def test_kill_switch_disables_everything(self):
        config.REDACT_SECRETS_ENABLED = False
        text = "sk-" + "a" * 30
        self.assertEqual(redaction.redact(text), text)

    def test_empty_and_none_safe(self):
        self.assertEqual(redaction.redact(""), "")
        self.assertEqual(redaction.redact(None), None)

    def test_never_raises_on_garbage(self):
        # Pathological input shouldn't blow up scan_secrets/redact.
        garbage = "(" * 5000 + "sk-" + "a" * 30
        try:
            redaction.redact(garbage)
        except Exception as e:  # pragma: no cover - this is the assertion
            self.fail(f"redact() raised on garbage input: {e}")

    def test_multiple_secrets_all_redacted(self):
        text = f"key sk-{'a' * 30} and card 4532015112830366"
        out = redaction.redact(text)
        self.assertIn("[REDACTED:openai_key]", out)
        self.assertIn("[REDACTED:card_number]", out)


# =====================================================================
# settings/logging_config.py - _RedactionFilter
# =====================================================================
class RedactionFilterTests(unittest.TestCase):
    def setUp(self):
        self._enabled = config.REDACT_SECRETS_ENABLED
        config.REDACT_SECRETS_ENABLED = True
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        config.REDACT_SECRETS_ENABLED = self._enabled
        lc.setup(data_dir=tempfile.mkdtemp(), env={}, force=True)

    def _log_path(self):
        return os.path.join(self.tmp, lc.LOG_FILENAME)

    def test_secret_scrubbed_from_log_file(self):
        lc.setup(self.tmp, env={}, force=True)
        secret = "sk-" + "a" * 30
        logging.getLogger("RedactTest").info("leaking key %s", secret)
        with open(self._log_path(), encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn(secret, content)
        self.assertIn("[REDACTED:openai_key]", content)

    def test_percent_sign_in_message_survives_multiple_handlers(self):
        # Two handlers (file + console) both run _RedactionFilter against
        # the SAME record. A bare "%" left in the already-rendered message
        # must not raise on the second handler's getMessage() call.
        lc.setup(self.tmp, env={}, force=True)
        try:
            logging.getLogger("RedactTest").info("battery at 87%% and key %s", "sk-" + "a" * 30)
        except Exception as e:  # pragma: no cover - this is the assertion
            self.fail(f"logging with a bare %% raised: {e}")
        with open(self._log_path(), encoding="utf-8") as f:
            content = f.read()
        self.assertIn("battery at 87%", content)
        self.assertIn("[REDACTED:openai_key]", content)

    def test_kill_switch_leaves_log_untouched(self):
        config.REDACT_SECRETS_ENABLED = False
        lc.setup(self.tmp, env={}, force=True)
        secret = "sk-" + "a" * 30
        logging.getLogger("RedactTest").info("key %s", secret)
        with open(self._log_path(), encoding="utf-8") as f:
            content = f.read()
        self.assertIn(secret, content)


# =====================================================================
# telemetry/trace.py - annotate()/mark()/end_turn() redact string metadata
# =====================================================================
class TraceRedactionTests(unittest.TestCase):
    def setUp(self):
        self._enabled = config.TELEMETRY_ENABLED
        self._redact_enabled = config.REDACT_SECRETS_ENABLED
        config.TELEMETRY_ENABLED = True
        config.REDACT_SECRETS_ENABLED = True
        trace._local.turn = None
        trace._local.begin_turn_id = None

    def tearDown(self):
        config.TELEMETRY_ENABLED = self._enabled
        config.REDACT_SECRETS_ENABLED = self._redact_enabled
        trace._local.turn = None
        trace._local.begin_turn_id = None

    def test_annotate_redacts_string_fields(self):
        trace.begin_turn(source="think")
        secret = "sk-" + "a" * 30
        trace.annotate(error=f"invalid key: {secret}")
        row = trace.end_turn(outcome="error")
        self.assertNotIn(secret, row["error"])
        self.assertIn("[REDACTED:openai_key]", row["error"])

    def test_mark_redacts_meta(self):
        trace.begin_turn(source="think")
        secret = "AKIA" + "A" * 16
        trace.mark("tool_call", args=f"token={secret}")
        row = trace.end_turn(outcome="ok")
        stages = row["stages"]
        self.assertEqual(len(stages), 1)
        serialized = str(stages[0])
        self.assertNotIn(secret, serialized)
        self.assertIn("[REDACTED:aws_access_key_id]", serialized)

    def test_end_turn_fields_redacted(self):
        trace.begin_turn(source="think")
        secret = "gsk_" + "a" * 25
        row = trace.end_turn(outcome="error", error=f"groq said: {secret}")
        self.assertNotIn(secret, row["error"])
        self.assertIn("[REDACTED:groq_key]", row["error"])

    def test_non_string_fields_pass_through(self):
        trace.begin_turn(source="think")
        row = trace.end_turn(outcome="ok", rounds=3)
        self.assertEqual(row["rounds"], 3)


# =====================================================================
# automation/health_check.py / skills/self_diagnostics.py
# =====================================================================
class ReportRedactionTests(unittest.TestCase):
    def setUp(self):
        self._enabled = config.REDACT_SECRETS_ENABLED
        config.REDACT_SECRETS_ENABLED = True

    def tearDown(self):
        config.REDACT_SECRETS_ENABLED = self._enabled

    def test_health_check_format_report_redacts(self):
        from automation import health_check

        secret = "sk-" + "a" * 30
        results = [
            health_check.CheckResult(
                name="LLM provider",
                status=health_check.Status.PROBLEM,
                message=f"invalid API key: {secret}",
            )
        ]
        report = health_check.format_report(results)
        self.assertNotIn(secret, report)
        self.assertIn("[REDACTED:openai_key]", report)

    def test_self_diagnostics_redacts(self):
        from unittest import mock
        from skills import self_diagnostics

        secret = "sk-" + "a" * 30
        with mock.patch.object(
            self_diagnostics, "_check_llm_provider",
            return_value=f"PROBLEM - invalid API key: {secret}",
        ), mock.patch.object(
            self_diagnostics, "_check_database", return_value="OK"
        ), mock.patch.object(
            self_diagnostics, "_check_disk", return_value="OK"
        ), mock.patch.object(
            self_diagnostics, "_check_voice_stack", return_value="OK"
        ):
            report = self_diagnostics.run_self_diagnostics()
        self.assertNotIn(secret, report)
        self.assertIn("[REDACTED:openai_key]", report)


if __name__ == "__main__":
    unittest.main()
