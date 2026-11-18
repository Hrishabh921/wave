"""
web/web_ops.py - Wave's outbound web layer.

Tier 3 / item 7 ("feature-depth gaps"): before this, every method here
opened its own ad-hoc requests.get()/webbrowser call with no retry, no
timeout discipline, and no check on *where* a URL actually points. This
rewrite adds three small, reusable pieces that every fetch in this file
(and get_weather()/get_news() specifically) goes through:

    _session      - one shared requests.Session with urllib3 Retry mounted
                    for http/https, so a 429/5xx gets retried with backoff
                    instead of failing the whole call outright.
    validate_url  - scheme allow-list (http/https only) plus a check that
                    the URL's host - literal IP or resolved hostname - isn't
                    loopback/private/link-local/reserved address space, so
                    nothing in this process can be tricked into fetching
                    "http://169.254.169.254/..." or "http://localhost/...".
    ttl_cache     - a tiny in-process cache decorator, used so repeated
                    get_weather()/get_news() calls for the same place/topic
                    within a short window don't re-hit the network every
                    time an LLM tool call or WeatherWorker asks.

search_google/ask_internet are unchanged in behavior - existing callers
(brain/groq_core.py's search_google and get_live_information skills) keep
working exactly as before.

Tier 16 adds two pieces on top:

fetch_page_text(url)  - "summarize this page" needs page text to hand the
                         LLM, and nothing in this file could get that
                         before. Goes through the same validate_url() SSRF
                         guard and shared _session as get_weather()/
                         get_news(). Uses BeautifulSoup to strip
                         script/style/nav chrome down to readable text when
                         it's installed, falling back to a small stdlib
                         html.parser.HTMLParser subclass (_TextExtractor)
                         when it isn't - see HAS_BS4 below.

play_on_youtube(query) - gap analysis: the old implementation, a single
                         pywhatkit.playonyt(query) call, finds its result
                         by scraping YouTube's search-results HTML
                         directly, which breaks whenever that page's
                         markup changes - exactly the "worked yesterday,
                         not today" fragility a user can't tell apart from
                         Wave itself being broken. yt-dlp is maintained
                         specifically to track YouTube's own changes (a
                         huge number of other tools depend on the same
                         extractor), so it's tried FIRST now, with
                         pywhatkit kept as the fallback for a bare install
                         that doesn't have yt-dlp - see HAS_YTDLP below.

Tier 19 adds a per-host circuit breaker in front of _session.get() (see
_guarded_get/_CircuitBreaker below), used by both get_weather() and
fetch_page_text() - the two direct GET call sites in this file.
WEB_MAX_RETRIES/backoff above already bounds ONE call's own attempts
against a host; this bounds repeated CALLS (separate tool calls, plan
steps, turns) against a host that's already shown itself to be down, so a
sustained outage fails fast instead of every single call paying the full
retry+timeout cost all over again.
"""
import ipaddress
import logging
import re
import socket
import threading
import time
import webbrowser
from functools import wraps
from html.parser import HTMLParser
from urllib.parse import quote, quote_plus, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from settings.config import config

try:
    # pywhatkit does its own internet-connectivity check AT IMPORT TIME
    # (pywhatkit.core.check_connection(), run as an __init__.py side
    # effect) and raises pywhatkit.core.exceptions.InternetException if it
    # can't reach whatever host it pings - which used to crash this whole
    # module, and so the whole app, on any machine with no internet yet
    # (a laptop opened before Wi-Fi connects, a captive portal, a
    # corporate network that blocks the one host it happens to probe) at
    # STARTUP, long before play_on_youtube() is ever called. A real user
    # hit exactly this: Wave wouldn't start at all, not just "YouTube
    # doesn't work." Caught with a plain `except Exception`, not
    # `BaseException` the way bench/redteam_runner.py's analogous
    # pyautogui/mouseinfo fix needed - pywhatkit's own exception classes
    # are ordinary Exception subclasses, verified against its source, not
    # assumed. play_on_youtube() below already wraps its one call site in
    # try/except, so `pywhatkit.playonyt(...)` on a None pywhatkit just
    # raises AttributeError there instead of a crash, and is caught the
    # same way an actual YouTube-side failure already was.
    import pywhatkit
    HAS_PYWHATKIT = True
except Exception:
    pywhatkit = None
    HAS_PYWHATKIT = False
    # `logger` (below) isn't defined yet this early in the module, so this
    # uses the stdlib logging call directly rather than moving the logger
    # definition up just to serve one message.
    logging.getLogger("WebManager").info(
        "pywhatkit unavailable (not installed, or its own import-time "
        "internet check failed) - play_on_youtube() will rely on yt-dlp alone.")

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

logger = logging.getLogger("WebManager")

_ALLOWED_SCHEMES = {"http", "https"}
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}

# Tier 19: shared between _build_session()'s per-call Retry (below) and
# _CircuitBreaker (further below) so the two layers agree on what counts
# as "this host/response looks transiently broken, not permanently wrong".
# 429/5xx are the server (or an intermediary) saying "try again later" -
# worth both an immediate retry AND counting against the host's circuit.
# A 404 or other 4xx is deliberately NOT in this set: it says the URL (or
# the request) is wrong, not that the host is unhealthy, and retrying - or
# tripping a breaker over - a 404 would be actively wrong (hammering the
# host for a page that will never exist, or locking out a host that's
# perfectly fine because one bad link was requested).
_TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Tier 17: pulled forward from Tier 20's "scanner hardening" bullet ("strip
# hidden HTML") - found while checking Wave's content_trust.py scanner
# against SARA's own specific published "uncovered" (undetected) red-team
# attack A014: a <p style="opacity:0.01">...</p> carrying an injection
# payload at near-zero opacity, invisible to a sighted human reading the
# rendered page but, before this, extracted as perfectly ordinary plain
# text by _extract_text() below - script/style/etc. above were always
# stripped, but nothing ever looked at an element's OWN rendering (style
# attribute or the `hidden` attribute) to decide whether it should be
# treated as real page content at all. A visually hidden element is never
# legitimate article copy - it's either decorative chrome (handled fine by
# just dropping it) or, per this exact SARA-documented shape, a smuggled
# instruction - so dropping it here can only ever remove noise/attacks,
# never genuine readable content a sighted user would have seen.
# opacity uses a NUMERIC comparison (parse the value, compare <= 0.05)
# rather than a string match on "0" - real pages hide content at values
# like 0.01 specifically to dodge a literal "opacity:0" check, and a
# genuinely low-but-nonzero opacity (0.01-0.05) is just as invisible to a
# human as literal 0. The 0.05 cutoff deliberately stays well under
# anything a page would use for an ordinary semi-transparent (but still
# visible) effect - a real watermark/overlay/disabled-state opacity is
# essentially always >= 0.3 in practice, nowhere near this threshold.
_HIDDEN_STYLE_DISPLAY_RE = re.compile(r"display\s*:\s*none\b", re.I)
_HIDDEN_STYLE_VISIBILITY_RE = re.compile(r"visibility\s*:\s*hidden\b", re.I)
_HIDDEN_STYLE_OPACITY_RE = re.compile(r"opacity\s*:\s*([\d.]+)", re.I)


def _style_hides_element(style: str) -> bool:
    """True if an inline `style` attribute value renders its element
    invisible (display:none, visibility:hidden, or a near-zero opacity)."""
    if not style:
        return False
    if _HIDDEN_STYLE_DISPLAY_RE.search(style) or _HIDDEN_STYLE_VISIBILITY_RE.search(style):
        return True
    m = _HIDDEN_STYLE_OPACITY_RE.search(style)
    if m:
        try:
            return float(m.group(1)) <= 0.05
        except ValueError:
            return False
    return False


def _attrs_hide_element(attrs) -> bool:
    """True if an `html.parser`-style attrs list (`[(name, value), ...]`,
    value possibly `None` for a bare boolean attribute) marks its element
    hidden - the `hidden` attribute, or a hiding inline `style`."""
    for name, value in attrs:
        name = (name or "").lower()
        if name == "hidden":
            return True
        if name == "style" and _style_hides_element(value or ""):
            return True
    return False


def validate_url(url: str) -> bool:
    """True if `url` is safe for Wave to fetch itself.

    Enforces a scheme allow-list (http/https only - no file://, data://,
    ftp://, ...) and blocks loopback/private/link-local/reserved IP
    literals, whether that's an IP written directly into the URL or a
    hostname that *resolves* to one (every address a DNS lookup returns is
    checked, not just the first - a hostname round-robining between a
    public and a private IP is rejected). This is the standard SSRF guard:
    without it, something that builds a URL from user/LLM-provided text
    (a location name, a redirect target) could be pointed at
    "http://169.254.169.254/..." (cloud metadata) or an internal service
    disguised behind an innocuous-looking hostname.

    Deliberately conservative - a URL that fails to parse, or a hostname
    that fails to resolve, is treated as unsafe rather than given a pass."""
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return False
    host = parsed.hostname
    if not host:
        return False

    try:
        literal_ip = ipaddress.ip_address(host)
        return _is_public_ip(literal_ip)
    except ValueError:
        pass  # not a literal IP - it's a hostname, resolve it below

    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    if not infos:
        return False
    for info in infos:
        raw_addr = info[4][0]
        try:
            resolved_ip = ipaddress.ip_address(raw_addr.split("%")[0])  # strip IPv6 zone id
        except ValueError:
            continue
        if not _is_public_ip(resolved_ip):
            return False
    return True


def _is_public_ip(ip) -> bool:
    return not (
        ip.is_loopback or ip.is_private or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def _build_session() -> requests.Session:
    """One Session, reused for every request this module makes - connection
    pooling plus the Retry below, instead of a fresh, unretried connection
    per call like the pre-Tier-3 code had."""
    session = requests.Session()
    retry = Retry(
        total=config.WEB_MAX_RETRIES,
        backoff_factor=0.5,
        status_forcelist=_TRANSIENT_STATUS_CODES,
        allowed_methods=frozenset(("GET", "HEAD")),  # never silently retry a POST
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_session = _build_session()


class _CircuitBreaker:
    """Per-host circuit breaker, standing in front of _session.get() (via
    _guarded_get() below) for repeated CALLS against the same host - a
    different problem from the Retry mounted on _session above, which only
    bounds how hard ONE call retries before giving up.

    Three states per host, tracked independently so one host's outage
    never affects another's:
      - closed    - normal; every call goes through to the network.
      - open      - failure_threshold consecutive transient failures were
                    just seen for this host; calls are rejected locally
                    (no network attempt at all) until cooldown_s elapses.
      - half-open - cooldown_s has elapsed since the circuit opened; the
                    NEXT call is let through as a trial. If it succeeds,
                    record_success() closes the circuit again. If it
                    fails, record_failure() re-opens it for another full
                    cooldown_s - a still-down host doesn't get hammered
                    just because the clock ran out once.

    A single success anywhere resets that host's failure streak to zero,
    so one flaky request followed by a good one never creeps toward
    tripping the breaker - only a genuine consecutive run of failures
    does. Thread-safe: WebManager methods run on whatever thread happens
    to be calling (WeatherWorker, an AIWorker's tool-loop, a planner
    step's own worker thread per Tier 19's handler-timeout change in
    planning/executor.py), not only the GUI thread."""

    def __init__(self, failure_threshold: int, cooldown_s: float):
        self._failure_threshold = failure_threshold
        self._cooldown_s = cooldown_s
        self._lock = threading.Lock()
        # host -> {"failures": int, "opened_at": float|None, "trial_inflight": bool}
        self._hosts = {}

    def _state(self, host: str) -> dict:
        state = self._hosts.get(host)
        if state is None:
            state = {"failures": 0, "opened_at": None, "trial_inflight": False}
            self._hosts[host] = state
        return state

    def allow(self, host: str) -> bool:
        """True if a call to `host` may proceed. False means the circuit is
        open and the caller should fail fast without touching the network."""
        with self._lock:
            state = self._state(host)
            if state["opened_at"] is None:
                return True  # closed
            elapsed = time.monotonic() - state["opened_at"]
            if elapsed < self._cooldown_s:
                return False  # still open
            if state["trial_inflight"]:
                # Someone else's half-open trial is already in flight for
                # this host; don't let a second caller pile on - treat
                # this one as still blocked until the trial resolves.
                return False
            state["trial_inflight"] = True  # half-open: let exactly this one through
            return True

    def record_success(self, host: str) -> None:
        with self._lock:
            state = self._state(host)
            state["failures"] = 0
            state["opened_at"] = None
            state["trial_inflight"] = False

    def record_failure(self, host: str) -> None:
        with self._lock:
            state = self._state(host)
            was_trial = state["trial_inflight"]
            state["trial_inflight"] = False
            if was_trial:
                # The half-open trial itself failed - the host is still
                # down, so re-open for a fresh cooldown without needing
                # failure_threshold more failures first.
                state["opened_at"] = time.monotonic()
                return
            state["failures"] += 1
            if state["failures"] >= self._failure_threshold and state["opened_at"] is None:
                state["opened_at"] = time.monotonic()


_circuit = _CircuitBreaker(
    failure_threshold=config.WEB_CIRCUIT_FAILURE_THRESHOLD,
    cooldown_s=config.WEB_CIRCUIT_COOLDOWN_S,
)


class _CircuitOpenError(requests.RequestException):
    """Raised by _guarded_get() when a host's circuit is open. Deliberately
    a requests.RequestException subclass - not a new exception family - so
    it flows straight through the exception handling every existing caller
    already has (fetch_page_text()'s `except requests.RequestException`,
    get_weather()'s generic `except Exception`) with zero new except-
    clauses needed anywhere downstream."""
    pass


def _guarded_get(url: str, **kwargs):
    """Drop-in replacement for _session.get(url, **kwargs) that checks and
    updates the per-host circuit breaker around the call.

    Only a requests.RequestException (connection refused, DNS failure,
    timeout, ...) or a response whose status is in _TRANSIENT_STATUS_CODES
    counts as a circuit-breaker failure - anything else (a clean 200, a
    404, a 403, ...) is a sign the host is reachable and answering, which
    is exactly what record_success() should see even if the specific
    request didn't get the caller what they wanted."""
    host = urlparse(url).hostname or url
    if not _circuit.allow(host):
        raise _CircuitOpenError(
            f"Skipping {host} - it's failed repeatedly recently and is "
            f"sitting out a cooldown before being tried again."
        )
    try:
        response = _session.get(url, **kwargs)
    except requests.RequestException:
        _circuit.record_failure(host)
        raise
    if response.status_code in _TRANSIENT_STATUS_CODES:
        _circuit.record_failure(host)
    else:
        _circuit.record_success(host)
    return response


def ttl_cache(ttl_seconds: float):
    """Tiny in-process cache decorator: the wrapped function's return value
    is cached per distinct (args, kwargs) for ttl_seconds. Not a general-
    purpose cache (no eviction/size limit - fine here, this is only ever
    applied to a couple of methods with a handful of distinct arguments
    like city names or news topics) but it IS thread-safe: WebManager
    methods are called from whatever thread happens to be running
    (WeatherWorker, an AIWorker's tool-calling turn, ...), not only the
    GUI thread.

    Only a successful call is cached - if the wrapped function raises, the
    exception just propagates (nothing is stored), so a transient failure
    is retried on the very next call instead of being "stuck" as a cached
    error for the rest of the TTL window. See get_weather()/_fetch_weather()
    for the pattern this is meant to be used with."""
    def decorator(fn):
        cache = {}
        lock = threading.Lock()

        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.monotonic()
            with lock:
                hit = cache.get(key)
                if hit is not None and (now - hit[1]) < ttl_seconds:
                    return hit[0]
            result = fn(*args, **kwargs)
            with lock:
                cache[key] = (result, now)
            return result

        wrapper.cache_clear = lambda: cache.clear()
        return wrapper
    return decorator


class _TextExtractor(HTMLParser):
    """Stdlib fallback for fetch_page_text() when beautifulsoup4 isn't
    installed - walks the HTML token stream once, collecting text outside
    of _SKIP_TAGS and outside any visually-hidden element (Tier 17 - see
    _style_hides_element()'s own comment above for why). script/style/...
    content is never real page copy, and including it would hand the LLM
    a pile of JS/CSS instead of the article. Much cruder than
    BeautifulSoup (no CSS-selector awareness, no recovery from markup
    beyond what html.parser itself tolerates) but needs nothing beyond the
    standard library, so fetch_page_text() still works on a bare install
    with no optional deps at all.

    Tracks a real stack of (tag_name, should_skip) rather than the single
    flat depth counter this class used before Tier 17 - a flat counter
    keyed only on "is this tag name in _SKIP_TAGS" can't correctly tell
    whether the CURRENT text sits inside a hidden ancestor once hiding
    became attribute-based (style="...") rather than purely tag-name-
    based: an ordinary <p> nested inside a hidden <div> must still be
    suppressed even though <p> itself triggers nothing. A stack checked
    for "any hidden entry, at any depth" handles that correctly for
    arbitrarily nested markup, the same way a real DOM traversal would."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._stack = []  # [(tag_name, should_skip_this_subtree), ...]
        self.chunks = []

    def _in_skip_context(self) -> bool:
        return any(skip for _, skip in self._stack)

    def handle_starttag(self, tag, attrs):
        should_skip = tag in _SKIP_TAGS or _attrs_hide_element(attrs)
        self._stack.append((tag, should_skip))

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i]
                break

    def handle_data(self, data):
        if not self._in_skip_context() and data.strip():
            self.chunks.append(data.strip())


def _extract_text(html: str) -> str:
    """HTML -> readable text, chrome stripped, blank lines collapsed. Tries
    BeautifulSoup first (a real parser, tolerant of malformed markup);
    falls back to _TextExtractor above when bs4 isn't installed."""
    if HAS_BS4:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(_SKIP_TAGS):
            tag.decompose()
        # Tier 17: also drop anything visually hidden (style="display:none"/
        # "visibility:hidden"/near-zero opacity, or the `hidden` attribute)
        # before extracting text - see _style_hides_element()'s own comment
        # above _SKIP_TAGS for why. soup.find_all(style=True) already only
        # matches elements that HAVE a style attribute at all, so this never
        # touches the (overwhelming) majority of ordinary tags.
        for tag in soup.find_all(style=True):
            if _style_hides_element(tag.get("style", "")):
                tag.decompose()
        for tag in soup.find_all(hidden=True):
            tag.decompose()
        text = soup.get_text("\n")
    else:
        parser = _TextExtractor()
        parser.feed(html)
        text = "\n".join(parser.chunks)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


_WEATHER_BASE = "https://wttr.in"


class WebManager:

    @classmethod
    def search_google(cls, query: str) -> str:
        try:
            # 🔧 FIX: query wasn't URL-encoded before - a search with spaces,
            # "&", "#", "+" etc. (or Hindi/Devanagari text) would produce a
            # broken or wrong URL. urllib.parse.quote_plus matches what
            # open_website()'s search path already does correctly.
            encoded = quote_plus(query)
            webbrowser.open(f"https://www.google.com/search?q={encoded}")
            return f"Searched Google for {query}."
        except Exception as e:
            logger.error(f"Google search failed: {e}")
            return "Failed to search Google."

    @classmethod
    def play_on_youtube(cls, query: str) -> str:
        """Opens the first YouTube search result for `query` in the
        default browser. Tries yt-dlp's search extractor first (see the
        module docstring for why); falls back to pywhatkit.playonyt() -
        the original implementation - when yt-dlp isn't installed, or if
        it raises or comes back with nothing. Never raises past here
        either way."""
        query = (query or "").strip()
        if not query:
            return "What would you like me to play on YouTube?"
        if HAS_YTDLP:
            try:
                video_url = cls._ytdlp_first_result(query)
                if video_url:
                    webbrowser.open(video_url)
                    return f"Playing {query} on YouTube."
                logger.warning(f"yt-dlp found no results for {query!r}, falling back to pywhatkit.")
            except Exception as e:
                logger.warning(f"yt-dlp search failed for {query!r}, falling back to pywhatkit: {e}")
        try:
            # Ye function direct YouTube par pehla result dhundhega aur Play kar dega!
            pywhatkit.playonyt(query)
            return f"Playing {query} directly on YouTube."
        except Exception as e:
            logger.error(f"YouTube play failed: {e}")
            return "Failed to play on YouTube."

    @staticmethod
    def _ytdlp_first_result(query: str) -> str:
        """First result's watch URL for `query`, via yt-dlp's `ytsearch1:`
        pseudo-extractor - a metadata-only lookup (extract_flat + no
        download, nothing more than the single video's id/url is pulled)
        that just gets play_on_youtube() the one URL it opens in a
        browser. Returns "" if yt-dlp finds nothing; any exception
        (network down, extractor error) propagates to the caller, which
        already wraps this call and falls back to pywhatkit on failure."""
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        entries = (info or {}).get("entries") or []
        if not entries:
            return ""
        raw = entries[0].get("url") or entries[0].get("id") or ""
        if not raw:
            return ""
        return raw if raw.startswith("http") else f"https://www.youtube.com/watch?v={raw}"

    @classmethod
    def ask_internet(cls, query: str) -> str:
        try:
            from duckduckgo_search import DDGS
            results = DDGS().text(query, max_results=2)
            if results:
                return " ".join([res.get("body", "") for res in results])
            return ""
        except Exception as e:
            logger.error(f"Internet search failed: {e}")
            return ""

    # ------------------------------------------------------------------
    # Tier 3 / item 7: weather
    # ------------------------------------------------------------------
    @classmethod
    def get_weather(cls, location: str = None) -> str:
        """Current conditions as a short string, e.g. 'Partly cloudy +24°C'.

        location is free text ('Jaipur', 'Mumbai, IN') - leave it None/empty
        to fall back to wttr.in's own IP-based geolocation, exactly like the
        pre-Tier-3 behavior (and like settings.config.WEATHER_DEFAULT_LOCATION
        left blank). Results are cached per distinct location for
        config.WEATHER_CACHE_TTL_S seconds and fetched through the shared,
        retrying session - this method itself never raises, always
        returning a human-readable string either way."""
        try:
            return cls._fetch_weather((location or "").strip())
        except Exception as e:
            logger.error(f"get_weather failed for {location or '(auto)'}: {e}")
            return "Weather data unavailable."

    @classmethod
    @ttl_cache(config.WEATHER_CACHE_TTL_S)
    def _fetch_weather(cls, location: str) -> str:
        path = f"/{quote(location, safe='')}" if location else "/"
        url = f"{_WEATHER_BASE}{path}?format=%C+%t"
        if not validate_url(url):
            raise ValueError(f"refusing to fetch an unsafe URL ({url!r})")
        res = _guarded_get(url, timeout=config.WEB_REQUEST_TIMEOUT_S)
        if res.status_code != 200 or not res.text.strip():
            raise RuntimeError(f"wttr.in returned HTTP {res.status_code}")
        return res.text.strip()

    # ------------------------------------------------------------------
    # Tier 3 / item 7: news - reuses the duckduckgo_search dependency
    # ask_internet already uses, just calling its .news() endpoint instead
    # of .text().
    # ------------------------------------------------------------------
    @classmethod
    def get_news(cls, topic: str = None, max_results: int = 5) -> list:
        """Recent headlines as a list of
        {"title", "body", "url", "source", "date"} dicts, most recent
        first. topic is free text ('cricket', 'AI regulation') - leave it
        None/empty for general top headlines. Cached per distinct
        (topic, max_results) for config.NEWS_CACHE_TTL_S seconds (shorter
        than weather's, since news actually moves). Never raises - returns
        [] on any failure (no internet, rate-limited, nothing found)."""
        try:
            capped = max(1, min(int(max_results or 5), 10))
            return cls._fetch_news((topic or "").strip(), capped)
        except Exception as e:
            logger.error(f"get_news failed for {topic or '(general)'}: {e}")
            return []

    @classmethod
    @ttl_cache(config.NEWS_CACHE_TTL_S)
    def _fetch_news(cls, topic: str, max_results: int) -> list:
        from duckduckgo_search import DDGS
        query = topic or "today's top news"
        results = DDGS().news(query, max_results=max_results)
        if not results:
            raise RuntimeError(f"no news results for {query!r}")
        return [
            {
                "title": r.get("title", ""),
                "body": r.get("body", ""),
                "url": r.get("url", ""),
                "source": r.get("source", ""),
                "date": r.get("date", ""),
            }
            for r in results
        ]

    # ------------------------------------------------------------------
    # Tier 16: summarize_page
    # ------------------------------------------------------------------
    @classmethod
    def fetch_page_text(cls, url: str) -> str:
        """Readable text pulled from a web page, truncated to
        config.WEB_SUMMARIZE_MAX_CHARS - the raw material
        brain/handlers/web.py's summarize_page skill hands the LLM, NOT
        something this method summarizes itself (that needs the LLM; this
        is pure extraction - see _extract_text()).

        Goes through the same validate_url() SSRF guard and shared,
        retrying _session as get_weather()/get_news(). Read-only: this
        only ever does a GET, never follows a redirect into a form or
        POSTs anything. Never raises - returns a human-readable string
        either way, including on failure, same contract as get_weather()."""
        url = (url or "").strip()
        if not url:
            return "What page would you like me to look at?"
        if not validate_url(url):
            return "I can't fetch that URL - it doesn't look like a safe, public web address."
        try:
            res = _guarded_get(
                url,
                timeout=config.WEB_REQUEST_TIMEOUT_S,
                headers={"User-Agent": "Mozilla/5.0 (compatible; WaveAssistant/1.0)"},
            )
        except requests.RequestException as e:
            logger.error(f"fetch_page_text failed for {url}: {e}")
            return f"Couldn't reach that page: {e}"
        if res.status_code != 200:
            return f"That page returned HTTP {res.status_code} - I couldn't read it."
        content_type = res.headers.get("Content-Type", "").lower()
        if "html" not in content_type and "text" not in content_type:
            return f"That doesn't look like a readable web page (content type: {content_type or 'unknown'})."
        try:
            text = _extract_text(res.text)
        except Exception as e:
            logger.error(f"fetch_page_text couldn't parse {url}: {e}")
            return "I fetched that page but couldn't make sense of its content."
        if not text:
            return "That page didn't have any readable text on it."
        if len(text) > config.WEB_SUMMARIZE_MAX_CHARS:
            text = text[:config.WEB_SUMMARIZE_MAX_CHARS] + " […truncated]"
        return text
