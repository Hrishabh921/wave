"""
brain/handlers/web.py - internet-facing information lookups: a generic web
search/question, dedicated weather and news, and opening a Google search in
the browser. Everything that returns real web content goes through
content_trust.wrap_untrusted() before coming back - see that module.

Tier 4: pulled out of brain/groq_core.py's WaveBrain class, which had grown to
1942+ lines with 50 _skill_* methods - a "no behavior change, pure refactor" per
the roadmap. Each function here is registered via the exact same @skill(...)
decorator at module-import time (brain/skills_registry.py's register() has
always documented its handlers as plain `fn(brain, args: dict) -> str` functions,
not bound methods - both _dispatch_tool() and _replay_confirmed() already call
handler(self, args) with the WaveBrain instance passed explicitly, so moving a
method out of the class into a plain top-level function taking `brain` instead of
`self` needed no change to either call site). See brain/handlers/__init__.py for
why these 8 files, and groq_core.py's own top for why it still imports this
package even though nothing in it is referenced by name.
"""
from brain import content_trust
from brain.skills_registry import skill
from web.web_ops import WebManager


@skill("search_google", "Opens a Google search in the browser for the given query.",
       {"query": {"type": "string", "description": "What to search for."}}, ["query"])
def _skill_search_google(brain, args: dict) -> str:
    return WebManager.search_google(args.get("query", ""))


@skill("get_live_information", "Fetches current/live information from the internet to answer a factual question (prices, scores, general current facts) — use this instead of guessing. For weather use get_weather, and for news/headlines use get_news - both are more reliable for those specifically.",
       {"query": {"type": "string", "description": "The question or topic to look up."}}, ["query"])
def _skill_get_live_information(brain, args: dict) -> str:
    data = WebManager.ask_internet(args.get("query", ""))
    if not data:
        return "I couldn't find live information on that right now."
    # Phase 7: this is someone else's web content, not the user's own
    # words - see brain/content_trust.py.
    return content_trust.wrap_untrusted(data, source="a web search")


@skill("get_weather", "Gets the current weather conditions for a place.",
       {"location": {"type": "string",
                      "description": "City or place name, e.g. 'Jaipur', 'New York'. Leave empty to use the user's current location (auto-detected)."}},
       [])
def _skill_get_weather(brain, args: dict) -> str:
    text = WebManager.get_weather(args.get("location") or None)
    return content_trust.wrap_untrusted(text, source="a weather service")


@skill("get_news", "Gets recent news headlines, optionally about a specific topic.",
       {"topic": {"type": "string",
                   "description": "What to get news about, e.g. 'cricket', 'AI regulation'. Leave empty for general top headlines."}},
       [])
def _skill_get_news(brain, args: dict) -> str:
    articles = WebManager.get_news(args.get("topic") or None)
    if not articles:
        return "I couldn't find any news on that right now."
    lines = [
        f"- {a['title']}" + (f" ({a['source']})" if a.get("source") else "")
        for a in articles if a.get("title")
    ]
    if not lines:
        return "I couldn't find any news on that right now."
    # Phase 7: this is someone else's web content, not the user's own
    # words - see brain/content_trust.py.
    return content_trust.wrap_untrusted("\n".join(lines), source="a news search")


@skill("summarize_page", "Fetches a web page's readable text (stripping scripts/styles/nav chrome) so "
       "you can summarize it - 'summarize this page', 'what does this article say', given a URL. This "
       "tool only fetches and extracts; write the actual summary yourself from what comes back.",
       {"url": {"type": "string", "description": "The page's URL, e.g. 'https://example.com/article'."}},
       ["url"])
def _skill_summarize_page(brain, args: dict) -> str:
    text = WebManager.fetch_page_text(args.get("url", ""))
    # Phase 7: a fetched page is someone else's content, not the user's own
    # words, same as get_live_information/get_weather/get_news above - see
    # brain/content_trust.py. Wrapping (and tainting the turn) applies even
    # though fetch_page_text() already failed gracefully on a bad/unsafe
    # URL - a short failure string is harmless to wrap, and skipping the
    # wrap on failure would be one more thing a future edit could get
    # wrong "just this once".
    return content_trust.wrap_untrusted(text, source="a web page")
