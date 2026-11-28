"""
brain/llm_provider.py — Phase 4: provider-agnostic LLM layer

Before this file, WaveBrain was welded to the Groq SDK: `from groq import Groq`
at import time, a hard-coded model name, and Ollama bolted on as a separate
code path. Now ONE line in .env decides which brain answers:

    LLM_PROVIDER=groq         # default - exactly what Wave did before
    LLM_PROVIDER=openai       # api.openai.com
    LLM_PROVIDER=openrouter   # openrouter.ai (hundreds of models behind one key)
    LLM_PROVIDER=ollama       # local models through Ollama's OpenAI-compatible /v1
    LLM_PROVIDER=custom       # anything else that speaks the OpenAI chat API
                              # (LM Studio, vLLM, llama.cpp server, Together, Mistral, ...)
                              # -> also set LLM_BASE_URL

Everything speaks the same wire protocol (POST {base_url}/chat/completions with
tools / tool_choice), so a single small client covers all of them. It is built
on `requests`, which Wave already depends on - no new package, nothing new for
PyInstaller to chase.

What this module guarantees
---------------------------
* `resolve_provider()` never raises. A missing key / URL is reported through
  `ProviderConfig.problem` so Wave can say so, then fall through its existing
  Ollama -> keyword-matcher chain instead of crashing.
* With no LLM_* variables at all, a legacy `GROQ_API_KEY` still selects Groq,
  the same models, and the official `groq` SDK when it's installed (zero change
  for existing setups). If the SDK isn't installed, Groq is reached through the
  generic client instead - `groq` is no longer a hard dependency.
* The client returns objects shaped like the SDK's (`resp.choices[0].message
  .tool_calls[i].function.arguments` ...) so WaveBrain's call sites didn't have
  to change.
* Parameter negotiation: the OpenAI-compatible world isn't uniform (reasoning
  models reject `temperature` and want `max_completion_tokens`; non-reasoning
  models reject `reasoning_effort`; some local servers reject `tool_choice`).
  On an HTTP 400 that names such a parameter, the client drops/renames just
  that parameter, retries, and remembers it per model - so you pay for the
  mismatch once, not on every message.
* Transient failures (429 / 5xx / connection reset) are retried with a short
  back-off that honours Retry-After; read timeouts are NOT retried (a voice
  assistant should fail over to the next tier quickly, not wait twice).
* API keys are never logged and never appear in error messages.
* Tier 2: `stream=True` returns a generator of ChatCompletionChunk objects
  (SSE, parsed as they arrive) instead of one ChatCompletion - shaped like
  the real SDKs' own streaming chunks so brain/groq_core.py's
  think_stream() doesn't need to know which client produced them. The
  same negotiation/retry behaviour above applies to OPENING the stream;
  once a chunk has actually started arriving there is no retrying mid-
  stream (see OpenAICompatClient._iter_stream).
"""
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

import requests

from settings.config import config  # noqa: F401  (guarantees load_dotenv() ran before we read the environment)

logger = logging.getLogger("LLMProvider")

PROVIDERS = ("groq", "openai", "openrouter", "gemini", "ollama", "custom")
_ALIASES = {
    "local": "ollama",
    "openai-compatible": "custom", "openai_compatible": "custom",
    "generic": "custom", "lmstudio": "custom", "vllm": "custom",
    "google": "gemini",
}

# Defaults. Groq's are the exact values Wave hard-coded before Phase 4.
# OpenAI/OpenRouter model ids move quickly - LLM_MODEL always wins, and a
# wrong default produces a clear "model not found" message, not a crash.
_PRESETS: Dict[str, Dict[str, Any]] = {
    "groq": dict(base_url="https://api.groq.com/openai/v1", key_vars=("GROQ_API_KEY",),
                 model="openai/gpt-oss-120b", vision_model="qwen/qwen3.8-27b", needs_key=True),
    "openai": dict(base_url="https://api.openai.com/v1", key_vars=("OPENAI_API_KEY",),
                   model="gpt-5.4-mini", vision_model=None, needs_key=True),
    "openrouter": dict(base_url="https://openrouter.ai/api/v1", key_vars=("OPENROUTER_API_KEY",),
                       model="openai/gpt-oss-120b", vision_model=None, needs_key=True,
                       headers={"X-Title": "Wave AI"}),
    # Tier 58: Google's OpenAI-compatible endpoint (chat completions with
    # tools, streaming) - a key from Google AI Studio. Model ids change often:
    # if this one is gone, WaveBrain picks the newest Flash the key can use.
    "gemini": dict(base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                   key_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY"), model="gemini-3.8-flash", vision_model=None,
                   needs_key=True),
    "ollama": dict(base_url=None, key_vars=(), model=None, vision_model=None, needs_key=False),
    "custom": dict(base_url=None, key_vars=(), model=None, vision_model=None, needs_key=False),
}
# Tier 58: who may stand in when every model of the main provider is
# rate-limited, in the order "auto" tries them (a key must be in .env).
BACKUP_PROVIDERS = ("gemini", "openrouter", "openai", "groq")

_PLACEHOLDER_RE = re.compile(r"^\s*(your[_\- ].*|<.*>|xxx+|changeme|none|null)\s*$", re.IGNORECASE)
_TRUTHY_OFF = ("false", "0", "no", "off")
_CONNECT_TIMEOUT = 10.0


# =============================================================================
# Configuration
# =============================================================================
@dataclass
class ProviderConfig:
    name: str
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    vision_model: str = ""
    timeout: float = 60.0
    max_retries: int = 2
    reasoning: str = "auto"                      # auto | off | low | medium | high
    headers: Dict[str, str] = field(default_factory=dict)
    use_sdk: bool = False                        # groq + default endpoint + SDK installed
    needs_key: bool = True
    key_var: str = "LLM_API_KEY"
    problem: Optional[str] = None                # why it can't be used, in plain words

    @property
    def configured(self) -> bool:
        return self.problem is None

    @property
    def host(self) -> str:
        return urlparse(self.base_url).netloc or self.base_url

    def masked_key(self) -> str:
        if not self.api_key:
            return "(none)"
        return "…" + self.api_key[-4:] if len(self.api_key) > 8 else "…"

    def describe(self) -> Dict[str, Any]:
        """Safe-to-display summary (the key is masked, the URL is host-only)."""
        return {
            "provider": self.name, "model": self.model, "vision_model": self.vision_model,
            "endpoint": self.host, "key": self.masked_key(), "configured": self.configured,
            "problem": self.problem, "sdk": self.use_sdk,
        }


def _clean(value: Optional[str]) -> str:
    return (value or "").strip()


def _env_float(env: Mapping[str, str], name: str, default: float, lo: float, hi: float) -> float:
    raw = _clean(env.get(name))
    if not raw:
        return default
    try:
        return max(lo, min(hi, float(raw)))
    except ValueError:
        logger.warning(f"{name}={raw!r} isn't a number - using {default}.")
        return default


def _normalize_base_url(url: str) -> str:
    url = _clean(url).rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if url.lower().endswith(suffix):
            url = url[: -len(suffix)]
    return url.rstrip("/")


def _is_local_host(host: str) -> bool:
    h = (host or "").split(":")[0].lower()
    return h in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or h.endswith(".local") or h.startswith("192.168.") \
        or h.startswith("10.") or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", h) is not None


def _usable_key(raw: str) -> str:
    raw = _clean(raw)
    return "" if (not raw or _PLACEHOLDER_RE.match(raw)) else raw


def resolve_provider(env: Optional[Mapping[str, str]] = None, groq_sdk_available: Optional[bool] = None) -> ProviderConfig:
    """Turns the environment into a ProviderConfig. Pure and never raises."""
    env = os.environ if env is None else env

    requested = _clean(env.get("LLM_PROVIDER")).lower()
    requested = _ALIASES.get(requested, requested)
    base_override = _normalize_base_url(env.get("LLM_BASE_URL", ""))
    if not requested:
        requested = "custom" if base_override else "groq"

    if requested not in PROVIDERS:
        return ProviderConfig(
            name=requested, problem=(f"Unknown LLM_PROVIDER '{requested}'. "
                                     f"Use one of: {', '.join(PROVIDERS)}."))

    preset = _PRESETS[requested]
    cfg = ProviderConfig(name=requested, needs_key=preset["needs_key"],
                         headers=dict(preset.get("headers") or {}))

    # ---- endpoint ----
    if requested == "ollama":
        host = _clean(env.get("OLLAMA_HOST")) or "http://localhost:11434"
        if not re.match(r"^https?://", host, re.IGNORECASE):
            host = "http://" + host
        cfg.base_url = base_override or _normalize_base_url(host.rstrip("/") + "/v1")
    else:
        cfg.base_url = base_override or preset["base_url"] or ""

    # ---- key ----
    cfg.key_var = "LLM_API_KEY"
    key = _usable_key(env.get("LLM_API_KEY", ""))
    if not key:
        for var in preset["key_vars"]:
            candidate = _usable_key(env.get(var, ""))
            if candidate:
                key, cfg.key_var = candidate, var
                break
        else:
            cfg.key_var = (preset["key_vars"] or ("LLM_API_KEY",))[0]
    cfg.api_key = key

    # ---- models ----
    default_model = preset["model"] or (
        (_clean(env.get("OLLAMA_MODEL")) or "llama3.1") if requested == "ollama" else "")
    cfg.model = _clean(env.get("LLM_MODEL")) or default_model
    cfg.vision_model = _clean(env.get("LLM_VISION_MODEL")) or preset["vision_model"] or cfg.model

    # ---- tuning ----
    default_timeout = 120.0 if requested == "ollama" else 60.0   # CPU inference is slow
    cfg.timeout = _env_float(env, "LLM_TIMEOUT", default_timeout, 1.0, 600.0)
    cfg.max_retries = int(_env_float(env, "LLM_MAX_RETRIES", 2, 0, 5))
    reasoning = _clean(env.get("LLM_REASONING_EFFORT")).lower() or "auto"
    cfg.reasoning = reasoning if reasoning in ("auto", "off", "low", "medium", "high") else "auto"
    if reasoning not in ("auto", "off", "low", "medium", "high"):
        logger.warning(f"LLM_REASONING_EFFORT={reasoning!r} ignored (use auto/off/low/medium/high).")

    # ---- can we actually use it? ----
    if not cfg.base_url:
        cfg.problem = "LLM_PROVIDER=custom needs LLM_BASE_URL (e.g. http://localhost:1234/v1)."
    elif not re.match(r"^https?://", cfg.base_url, re.IGNORECASE):
        cfg.problem = f"LLM_BASE_URL must start with http:// or https:// (got '{cfg.base_url}')."
    elif cfg.needs_key and not cfg.api_key:
        holder = next((v for v in ("LLM_API_KEY", *preset["key_vars"]) if _clean(env.get(v))), None)
        if holder:
            cfg.key_var = holder
        why = "still has the placeholder value from .env.example" if holder else "is missing"
        cfg.problem = f"{cfg.key_var} {why}. Add your {requested} API key to .env (or set LLM_API_KEY)."
    elif not cfg.model:
        cfg.problem = "No model configured. Set LLM_MODEL in .env."

    if cfg.configured and cfg.api_key and cfg.base_url.lower().startswith("http://") \
            and not _is_local_host(urlparse(cfg.base_url).netloc):
        logger.warning("LLM_BASE_URL is plain http:// to a non-local host - your API key travels unencrypted.")

    # ---- official SDK only for stock Groq (identical to pre-Phase-4 behaviour) ----
    if requested == "groq" and not base_override and cfg.configured:
        if groq_sdk_available is None:
            try:
                import groq  # noqa: F401
                groq_sdk_available = True
            except ImportError:
                groq_sdk_available = False
        cfg.use_sdk = bool(groq_sdk_available)
    return cfg


def resolve_backup_provider(env: Optional[Mapping[str, str]] = None) -> Optional[ProviderConfig]:
    """Tier 58: the provider a request moves to when every model of the main
    one is rate-limited (Groq's free tier: 8,000 tokens a minute per model)
    or the main one is down. LLM_BACKUP_PROVIDER: "auto" (default - the
    first of BACKUP_PROVIDERS, other than the main one, whose key is in
    .env: GEMINI_API_KEY first), "off", or a provider name; LLM_BACKUP_MODEL
    picks its model. The main provider's LLM_* settings (LLM_API_KEY,
    LLM_MODEL, ...) never leak into the backup. None when there's no usable
    backup. Pure and never raises."""
    env = os.environ if env is None else env
    want = _clean(env.get("LLM_BACKUP_PROVIDER")).lower() or "auto"
    want = _ALIASES.get(want, want)
    if want in _TRUTHY_OFF or want == "none":
        return None
    main = _ALIASES.get(_clean(env.get("LLM_PROVIDER")).lower(), _clean(env.get("LLM_PROVIDER")).lower()) or "groq"
    if want == "auto":
        want = next((p for p in BACKUP_PROVIDERS if p != main
                     and any(_usable_key(env.get(v, "")) for v in _PRESETS[p]["key_vars"])), "")
        if not want:
            return None
    if want not in BACKUP_PROVIDERS:
        logger.warning(f"LLM_BACKUP_PROVIDER={want!r} can't be a backup (use one of {', '.join(BACKUP_PROVIDERS)}).")
        return None
    own = {k: v for k, v in env.items() if not k.startswith("LLM_")}
    own["LLM_PROVIDER"] = want
    if _clean(env.get("LLM_BACKUP_MODEL")):
        own["LLM_MODEL"] = _clean(env.get("LLM_BACKUP_MODEL"))
    cfg = resolve_provider(own, groq_sdk_available=False)
    if not cfg.configured:
        logger.info(f"Backup LLM {want} not used: {cfg.problem}")
        return None
    # A stand-in must answer quickly or step aside: checked live, Gemini's
    # newest Flash answered "high demand" (503) for minutes, and the client's
    # usual retries with backoff turned one word into a 67 s wait.
    cfg.timeout = _env_float(env, "LLM_BACKUP_TIMEOUT", 12.0, 3.0, 120.0)
    cfg.max_retries = int(_env_float(env, "LLM_BACKUP_MAX_RETRIES", 0, 0, 3))
    return cfg


def best_flash_model(model_ids, lite: bool = False) -> Optional[str]:
    """Tier 58: the newest plain Gemini Flash among `model_ids` (as the API
    lists them, "models/gemini-3.8-flash" or bare) - not an image/audio/
    embedding/preview variant; a Flash-Lite only when there's no Flash.
    lite=True: the newest Flash-Lite instead (None if there's none)."""
    names = [str(i).split("/", 1)[1] if str(i).startswith("models/") else str(i) for i in model_ids or ()]

    def version(name):
        m = re.match(r"gemini-(\d+(?:\.\d+)?)", name)
        return float(m.group(1)) if m else 0.0
    special = ("image", "tts", "audio", "live", "embedding", "vision", "exp", "preview", "thinking")
    flash = [n for n in names if n.startswith("gemini-") and "flash" in n and not any(s in n for s in special)]
    if lite:
        pool = [n for n in flash if n.endswith("flash-lite")]
    else:
        pool = [n for n in flash if "lite" not in n] or flash
    return max(pool, key=version) if pool else None


# =============================================================================
# Response objects (same attribute shape as the Groq/OpenAI SDKs)
# =============================================================================
@dataclass
class ToolFunction:
    name: str
    arguments: str


@dataclass
class ToolCall:
    id: str
    function: ToolFunction
    type: str = "function"


@dataclass
class Message:
    content: Optional[str]
    tool_calls: Optional[List[ToolCall]] = None
    role: str = "assistant"


@dataclass
class Choice:
    message: Message
    finish_reason: Optional[str] = None
    index: int = 0


@dataclass
class ChatCompletion:
    choices: List[Choice]
    model: str = ""
    usage: Optional[dict] = None
    id: str = ""


# ── Tier 2: streaming response objects ──────────────────────────────────
# Mirror the real SDKs' own streaming chunk shape (chunk.choices[0].delta
# .content / .tool_calls[i].index/.id/.function.name/.function.arguments /
# the round's finish_reason) so brain/groq_core.py's think_stream() can
# handle a chunk from the real groq SDK and a chunk from OpenAICompatClient
# identically - it never needs to know which one produced it.
@dataclass
class ToolCallDeltaFunction:
    name: Optional[str] = None
    arguments: Optional[str] = None


@dataclass
class ToolCallDelta:
    index: int
    id: Optional[str] = None
    type: Optional[str] = None
    function: ToolCallDeltaFunction = field(default_factory=ToolCallDeltaFunction)


@dataclass
class ChoiceDelta:
    role: Optional[str] = None
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCallDelta]] = None


@dataclass
class ChunkChoice:
    delta: ChoiceDelta
    finish_reason: Optional[str] = None
    index: int = 0


@dataclass
class ChatCompletionChunk:
    choices: List[ChunkChoice]
    model: str = ""
    id: str = ""


class LLMError(Exception):
    """Raised for every provider-side failure. `str(e)` is safe to show to the
    user (no key, no payload)."""

    def __init__(self, message: str, *, status: Optional[int] = None, kind: str = "error",
                 retriable: bool = False, param: Optional[str] = None, detail: str = "",
                 retry_after: Optional[float] = None):
        super().__init__(message)
        self.status, self.kind, self.retriable = status, kind, retriable
        self.param, self.detail, self.retry_after = param, detail, retry_after


def _parse_completion(data: Any, requested_model: str) -> ChatCompletion:
    if not isinstance(data, dict) or not data.get("choices"):
        raise LLMError("The provider replied, but with no completion in it.", kind="bad_response")
    choices = []
    for i, ch in enumerate(data["choices"]):
        msg = (ch or {}).get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):  # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict)) or None
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, (dict, list)):
                args = json.dumps(args)
            calls.append(ToolCall(
                id=(tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
                function=ToolFunction(name=fn.get("name") or "", arguments=args if isinstance(args, str) and args else "{}"),
            ))
        choices.append(Choice(
            message=Message(content=content, tool_calls=calls or None, role=msg.get("role") or "assistant"),
            finish_reason=ch.get("finish_reason"), index=ch.get("index", i)))
    return ChatCompletion(choices=choices, model=data.get("model") or requested_model,
                          usage=data.get("usage"), id=data.get("id") or "")


def _parse_stream_chunk(data: Any, requested_model: str) -> ChatCompletionChunk:
    """One parsed `data: {...}` SSE payload -> ChatCompletionChunk. Unlike
    _parse_completion this never raises on an empty/odd shape - a single
    malformed chunk shouldn't kill an otherwise-good stream, so a chunk
    with no usable choices just comes back with an empty choices list
    (the caller's `chunk.choices[0]` access on something we didn't
    produce can't happen; see _iter_stream, which only ever hands chunks
    it built here to the caller)."""
    choices = []
    for i, ch in enumerate((data or {}).get("choices") or []):
        d = (ch or {}).get("delta") or {}
        content = d.get("content")
        if isinstance(content, list):  # a few providers send content parts even in deltas
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict)) or None
        tool_calls = None
        raw_calls = d.get("tool_calls")
        if raw_calls:
            tool_calls = []
            for tc in raw_calls:
                fn = (tc or {}).get("function") or {}
                tool_calls.append(ToolCallDelta(
                    index=(tc or {}).get("index", 0),
                    id=(tc or {}).get("id"),
                    type=(tc or {}).get("type"),
                    function=ToolCallDeltaFunction(name=fn.get("name"), arguments=fn.get("arguments")),
                ))
        choices.append(ChunkChoice(
            delta=ChoiceDelta(role=d.get("role"), content=content, tool_calls=tool_calls),
            finish_reason=(ch or {}).get("finish_reason"), index=(ch or {}).get("index", i),
        ))
    if not choices:
        choices = [ChunkChoice(delta=ChoiceDelta())]
    return ChatCompletionChunk(choices=choices, model=(data or {}).get("model") or requested_model,
                               id=(data or {}).get("id") or "")


# =============================================================================
# The client
# =============================================================================
_DROPPABLE = ("temperature", "top_p", "reasoning_effort", "reasoning", "tool_choice",
              "parallel_tool_calls", "frequency_penalty", "presence_penalty", "seed")
_REJECT_WORDS = ("unsupported", "not supported", "unknown", "unrecognized", "invalid", "does not support",
                 "extra inputs", "not permitted", "only the default", "not allowed", "unexpected")
_MAX_NEGOTIATIONS = 4
_MAX_RETRY_WAIT = 5.0   # seconds; a longer Retry-After fails over to the next tier instead


class _Completions:
    def __init__(self, client: "OpenAICompatClient"):
        self._client = client

    def create(self, *, messages, model=None, tools=None, tool_choice=None, temperature=None,
               max_tokens=None, extra_body=None, stream=False, timeout=None, **extra):
        if stream:
            # Tier 2: returns a generator of ChatCompletionChunk instead of
            # one ChatCompletion - see OpenAICompatClient._create_stream.
            return self._client._create_stream(messages=messages, model=model, tools=tools, tool_choice=tool_choice,
                                               temperature=temperature, max_tokens=max_tokens,
                                               extra_body=extra_body, extra=extra, timeout=timeout)
        # Phase 6: `timeout` is pulled out as its own named parameter (rather
        # than falling into **extra) specifically so it does NOT end up as a
        # JSON body field - see _build_payload, which dumps everything left
        # in `extra` straight into the request payload. A caller bounding
        # one call against a governance deadline (brain/groq_core.py's
        # _chat_completion) gets an actual shorter HTTP read timeout here,
        # not a server-side 400 for an unrecognized "timeout" parameter.
        return self._client._create(messages=messages, model=model, tools=tools, tool_choice=tool_choice,
                                    temperature=temperature, max_tokens=max_tokens,
                                    extra_body=extra_body, extra=extra, timeout=timeout)


class _Chat:
    def __init__(self, client: "OpenAICompatClient"):
        self.completions = _Completions(client)


class OpenAICompatClient:
    """Minimal OpenAI-chat-completions client. Mirrors `client.chat.completions.create(...)`."""

    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.chat = _Chat(self)
        self._tls = threading.local()
        self._quirks: Dict[str, Dict[str, Any]] = {}
        self._quirk_lock = threading.Lock()

    # ---------------- plumbing ----------------
    def _session(self) -> requests.Session:
        s = getattr(self._tls, "session", None)
        if s is None:
            s = self._tls.session = requests.Session()
        return s

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        h.update(self.cfg.headers)
        if self.cfg.api_key:
            h["Authorization"] = f"Bearer {self.cfg.api_key}"
        return h

    def _quirk(self, model: str) -> Dict[str, Any]:
        with self._quirk_lock:
            return self._quirks.setdefault(model, {"drop": set(), "max_tokens_key": "max_tokens"})

    def quirks(self) -> Dict[str, Dict[str, Any]]:
        """What this client has learned about each model's accepted parameters (for tests / debugging)."""
        with self._quirk_lock:
            return {m: {"drop": sorted(q["drop"]), "max_tokens_key": q["max_tokens_key"]}
                    for m, q in self._quirks.items()}

    # ---------------- request building ----------------
    def _build_payload(self, *, messages, model, tools, tool_choice, temperature, max_tokens, extra_body, extra) -> dict:
        q = self._quirk(model)
        payload: Dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload[q["max_tokens_key"]] = max_tokens
        payload.update({k: v for k, v in (extra or {}).items() if v is not None})
        payload.update(extra_body or {})

        mode = self.cfg.reasoning
        if mode == "off":
            payload.pop("reasoning_effort", None)
            payload.pop("reasoning", None)
        elif mode in ("low", "medium", "high") and "reasoning_effort" in payload:
            payload["reasoning_effort"] = mode
        for name in q["drop"]:
            payload.pop(name, None)
        return payload

    # ---------------- sending ----------------
    @staticmethod
    def _error_fields(resp: requests.Response) -> Tuple[str, Optional[str], str]:
        """(message, param, raw_snippet) from an error response body."""
        text = (resp.text or "")[:400]
        try:
            body = resp.json()
        except ValueError:
            return text.strip(), None, text
        err = body.get("error", body) if isinstance(body, dict) else body
        if isinstance(err, dict):
            return str(err.get("message") or err.get("detail") or text).strip(), err.get("param"), text
        return str(err).strip()[:400], None, text

    def _raise_for(self, resp: requests.Response, model: str):
        status = resp.status_code
        msg, param, raw = self._error_fields(resp)
        who = self.cfg.name
        retry_after = None
        try:
            retry_after = float(resp.headers.get("Retry-After", ""))
        except ValueError:
            pass
        if status in (401, 403):
            raise LLMError(f"{who} rejected the API key (HTTP {status}). Check {self.cfg.key_var} in your .env.",
                           status=status, kind="auth", detail=raw)
        if status == 404:
            raise LLMError(f"{who} couldn't find model '{model}' or the endpoint (HTTP 404). "
                           f"Check LLM_MODEL / LLM_BASE_URL. Server said: {msg[:200]}",
                           status=status, kind="not_found", detail=raw)
        if status in (408, 409, 429):
            raise LLMError(f"{who} is rate-limiting or busy (HTTP {status}). {msg[:200]}".strip(),
                           status=status, kind="rate_limit", retriable=True, retry_after=retry_after, detail=raw)
        if status >= 500:
            raise LLMError(f"{who} had a server error (HTTP {status}). {msg[:200]}".strip(),
                           status=status, kind="server", retriable=True, retry_after=retry_after, detail=raw)
        raise LLMError(f"{who} rejected the request (HTTP {status}): {msg[:300]}",
                       status=status, kind="bad_request", param=param, detail=msg)

    def _post(self, payload: dict, timeout: Optional[float], stream: bool):
        """Shared network call for both _send (one-shot) and _open_stream
        (SSE) - the exception-to-LLMError mapping is identical either
        way; only what happens to the response afterwards differs."""
        url = f"{self.cfg.base_url}/chat/completions"
        # Phase 6: an explicit per-call `timeout` (governance's remaining
        # budget) only ever SHORTENS the read timeout, never lengthens it
        # past the provider's own configured LLM_TIMEOUT - and is floored at
        # 1s so a near-zero governance budget can't hand `requests` an
        # invalid non-positive timeout.
        read_timeout = self.cfg.timeout if timeout is None else max(1.0, min(self.cfg.timeout, timeout))
        try:
            return self._session().post(url, json=payload, headers=self._headers(),
                                        timeout=(_CONNECT_TIMEOUT, read_timeout), stream=stream)
        except requests.exceptions.ConnectTimeout:
            raise LLMError(f"Timed out connecting to {self.cfg.host}.", kind="connection", retriable=True)
        except requests.exceptions.ReadTimeout:
            hint = "raise LLM_TIMEOUT for slow/local models" if timeout is None else \
                   "this call was bounded by the tool loop's remaining time budget (TOOL_LOOP_TIMEOUT_S)"
            raise LLMError(f"{self.cfg.name} didn't answer within {read_timeout:g}s ({hint}).", kind="timeout")
        except requests.exceptions.SSLError:
            raise LLMError(f"TLS/SSL problem talking to {self.cfg.host} - check LLM_BASE_URL (http vs https).",
                           kind="connection")
        except requests.exceptions.ConnectionError:
            hint = " Is the Ollama app running?" if self.cfg.name == "ollama" else ""
            raise LLMError(f"Couldn't reach {self.cfg.host}.{hint}", kind="connection", retriable=True)
        except requests.exceptions.RequestException as e:
            raise LLMError(f"Network error talking to {self.cfg.host}: {type(e).__name__}.", kind="connection")

    def _send(self, payload: dict, timeout: Optional[float] = None) -> dict:
        resp = self._post(payload, timeout, stream=False)
        if resp.status_code != 200:
            self._raise_for(resp, payload.get("model", ""))
        try:
            return resp.json()
        except ValueError:
            raise LLMError(f"{self.cfg.name} sent a reply that wasn't JSON.", kind="bad_response")

    def _open_stream(self, payload: dict, timeout: Optional[float] = None) -> requests.Response:
        """Tier 2: like _send, but opens an SSE response instead of
        reading one JSON body. Raises the exact same LLMErrors _send does
        if the server rejects the request outright (before any content
        streams) - a caller never gets a generator back for a request
        that failed at the HTTP level, which is what lets
        _create_stream's negotiation/retry loop work the same way
        _create's already does."""
        resp = self._post(payload, timeout, stream=True)
        if resp.status_code != 200:
            try:
                self._raise_for(resp, payload.get("model", ""))
            finally:
                resp.close()
        return resp

    def _iter_stream(self, resp: requests.Response, requested_model: str):
        """Turns one open `text/event-stream` response into a generator of
        ChatCompletionChunk objects. Any problem reading the body once
        streaming has actually started (a dropped connection, a mid-
        stream error event) becomes a normal LLMError raised out of the
        generator at the point it's hit - think_stream()'s caller sees
        the same kind of failure it would from a non-streaming call,
        just potentially after some good chunks already arrived. There is
        deliberately no retry in here: re-opening the request would mean
        either silently duplicating or silently dropping whatever was
        already yielded, and the caller is in a far better position to
        decide what to do with a partial answer than this layer is."""
        try:
            with resp:
                for raw_line in resp.iter_lines(decode_unicode=True):
                    if not raw_line or raw_line.startswith(":"):
                        continue  # SSE keep-alive / comment line
                    if not raw_line.startswith("data:"):
                        continue
                    data_str = raw_line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        return
                    try:
                        data = json.loads(data_str)
                    except ValueError:
                        continue  # a stray non-JSON line shouldn't kill an otherwise-good stream
                    if isinstance(data, dict) and data.get("error"):
                        err = data["error"]
                        msg = err.get("message") if isinstance(err, dict) else str(err)
                        raise LLMError(f"{self.cfg.name} reported an error mid-stream: {msg}", kind="server")
                    yield _parse_stream_chunk(data, requested_model)
        except requests.exceptions.ChunkedEncodingError:
            raise LLMError(f"{self.cfg.name}'s connection dropped mid-stream.", kind="connection")
        except requests.exceptions.RequestException as e:
            raise LLMError(f"Network error reading {self.cfg.name}'s stream: {type(e).__name__}.", kind="connection")

    def _negotiate(self, payload: dict, err: LLMError) -> bool:
        """Adjust `payload` (and remember it) if `err` names a parameter this model
        doesn't accept. Returns whether anything changed."""
        model = payload.get("model", "")
        q = self._quirk(model)
        msg = f"{err.detail} {err}".lower()
        param = (err.param or "").lower()
        changed = False

        if "max_completion_tokens" in msg and "max_tokens" in payload and "max_completion_tokens" not in payload:
            payload["max_completion_tokens"] = payload.pop("max_tokens")
            q["max_tokens_key"] = "max_completion_tokens"
            changed = True
        elif ("max_completion_tokens" in payload and (param == "max_completion_tokens" or "max_completion_tokens" in msg)
              and any(w in msg for w in _REJECT_WORDS)):
            payload["max_tokens"] = payload.pop("max_completion_tokens")
            q["max_tokens_key"] = "max_tokens"
            changed = True

        rejected = any(w in msg for w in _REJECT_WORDS)
        for name in _DROPPABLE:
            if name in payload and (param == name or (rejected and re.search(rf"\b{name}\b", msg))):
                payload.pop(name, None)
                q["drop"].add(name)
                changed = True
        if changed:
            logger.info(f"{self.cfg.name}/{model}: adjusted request parameters after a 400 "
                        f"(dropped={sorted(q['drop'])}, max_tokens_key={q['max_tokens_key']}).")
        return changed

    def _create(self, *, messages, model, tools, tool_choice, temperature, max_tokens, extra_body, extra,
               timeout: Optional[float] = None) -> ChatCompletion:
        model = model or self.cfg.model
        payload = self._build_payload(messages=messages, model=model, tools=tools, tool_choice=tool_choice,
                                      temperature=temperature, max_tokens=max_tokens,
                                      extra_body=extra_body, extra=extra)
        negotiations = transient = 0
        while True:
            try:
                return _parse_completion(self._send(payload, timeout=timeout), model)
            except LLMError as e:
                if e.kind == "bad_request" and negotiations < _MAX_NEGOTIATIONS and self._negotiate(payload, e):
                    negotiations += 1
                    continue
                limit = min(1, self.cfg.max_retries) if e.kind == "connection" else self.cfg.max_retries
                too_long = e.retry_after is not None and e.retry_after > _MAX_RETRY_WAIT
                if e.retriable and transient < limit and not too_long:
                    delay = e.retry_after if e.retry_after is not None else 0.8 * (2 ** transient)
                    logger.info(f"{self.cfg.name}: transient error ({e.kind}), retrying in {delay:.1f}s")
                    transient += 1
                    time.sleep(delay)
                    continue
                raise

    def _create_stream(self, *, messages, model, tools, tool_choice, temperature, max_tokens, extra_body, extra,
                       timeout: Optional[float] = None):
        """Tier 2: streaming counterpart to _create. Same parameter-
        negotiation/transient-retry loop, but it only ever runs against
        OPENING the connection - once _open_stream hands back a 200
        response, this returns a generator over it immediately (see
        _iter_stream) rather than reading/parsing a complete body. A
        caller only ever receives a generator for a request the server
        actually accepted; anything rejected up front (a bad parameter, a
        transient 429/5xx) is negotiated/retried exactly like a normal
        _create call, invisibly to think_stream()."""
        model = model or self.cfg.model
        payload = self._build_payload(messages=messages, model=model, tools=tools, tool_choice=tool_choice,
                                      temperature=temperature, max_tokens=max_tokens,
                                      extra_body=extra_body, extra=extra)
        payload["stream"] = True
        negotiations = transient = 0
        while True:
            try:
                resp = self._open_stream(payload, timeout=timeout)
                return self._iter_stream(resp, model)
            except LLMError as e:
                if e.kind == "bad_request" and negotiations < _MAX_NEGOTIATIONS and self._negotiate(payload, e):
                    negotiations += 1
                    continue
                limit = min(1, self.cfg.max_retries) if e.kind == "connection" else self.cfg.max_retries
                too_long = e.retry_after is not None and e.retry_after > _MAX_RETRY_WAIT
                if e.retriable and transient < limit and not too_long:
                    delay = e.retry_after if e.retry_after is not None else 0.8 * (2 ** transient)
                    logger.info(f"{self.cfg.name}: transient error ({e.kind}) opening stream, retrying in {delay:.1f}s")
                    transient += 1
                    time.sleep(delay)
                    continue
                raise

    def model_ids(self) -> List[str]:
        """Tier 58: the model ids GET /models lists ([] when it can't be read)."""
        try:
            resp = self._session().get(f"{self.cfg.base_url}/models", headers=self._headers(),
                                       timeout=(_CONNECT_TIMEOUT, min(self.cfg.timeout, 15)))
            if resp.status_code != 200:
                return []
            data = resp.json().get("data") or []
            return [str(d["id"]) for d in data if isinstance(d, dict) and d.get("id")]
        except Exception:
            return []

    # ---------------- health check ----------------
    def ping(self) -> Tuple[bool, str, int]:
        """(ok, human message, latency ms). Never raises."""
        t0 = time.monotonic()

        def ms():
            return int((time.monotonic() - t0) * 1000)
        probe = "/auth/key" if self.cfg.name == "openrouter" else "/models"
        try:
            resp = self._session().get(f"{self.cfg.base_url}{probe}", headers=self._headers(),
                                       timeout=(_CONNECT_TIMEOUT, min(self.cfg.timeout, 15)))
            if resp.status_code == 200:
                return True, f"Connected to {self.cfg.host}.", ms()
            if resp.status_code in (401, 403):
                return False, f"{self.cfg.name} rejected the API key (HTTP {resp.status_code}).", ms()
            if resp.status_code not in (404, 405):
                return False, f"{self.cfg.host} answered HTTP {resp.status_code}.", ms()
        except requests.exceptions.RequestException:
            return False, f"Couldn't reach {self.cfg.host}.", ms()
        # /models isn't offered everywhere (some custom servers) - try one tiny completion instead.
        try:
            self._create(messages=[{"role": "user", "content": "ping"}], model=self.cfg.model, tools=None,
                         tool_choice=None, temperature=None, max_tokens=1, extra_body=None, extra={})
            return True, f"Connected to {self.cfg.host}.", ms()
        except LLMError as e:
            return False, str(e), ms()


def build_client(cfg: ProviderConfig):
    """The object WaveBrain calls `.chat.completions.create(...)` on, or None if
    the provider isn't usable (see cfg.problem)."""
    if not cfg.configured:
        return None
    if cfg.use_sdk:
        try:
            from groq import Groq
            # Tier 29: no SDK-level retries. On a 429 the SDK slept out
            # Retry-After (~30 s on the free tier) twice before giving up;
            # WaveBrain._llm_create() now moves to a backup model at once
            # and retries 5xx/connection errors itself, once, quickly.
            try:
                return Groq(api_key=cfg.api_key, max_retries=0)
            except TypeError:                 # an SDK (or stand-in) without that option
                return Groq(api_key=cfg.api_key)
        except Exception as e:  # SDK vanished or refused to build -> generic client still works
            logger.info(f"groq SDK unavailable ({type(e).__name__}); using the generic OpenAI-compatible client.")
    return OpenAICompatClient(cfg)


def check_connection(client, cfg: ProviderConfig) -> Tuple[bool, str, int]:
    """Provider-agnostic health check used by the Settings page. Never raises."""
    if client is None:
        return False, cfg.problem or "No provider configured.", 0
    if hasattr(client, "ping"):
        return client.ping()
    t0 = time.monotonic()
    try:  # official SDK client
        client.models.list()
        return True, f"Connected to {cfg.host}.", int((time.monotonic() - t0) * 1000)
    except Exception as e:
        return False, f"{cfg.name} check failed: {type(e).__name__}.", int((time.monotonic() - t0) * 1000)
