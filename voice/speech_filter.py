"""
voice/speech_filter.py — Tier 11: which part of a reply gets SPOKEN
=====================================================================

The chat bubble shows a reply as markdown; the voice shouldn't read a code
block, table pipes or a |---|---| separator row out loud. The old
ui/main_window.py _plain_text_for_speech() handled that for a finished
reply, with regexes over the whole string. A streamed reply arrives in
arbitrary chunks - a ``` fence can be split across two of them - so this is
an incremental, line-aware filter instead:

    f = SpeechFilter()
    for chunk in stream:
        speakable = f.feed(chunk)     # text that is safe to speak so far
    speakable += f.flush()            # whatever was still held back

Rules (per line):
  * ordinary text is passed through immediately, character for character
    as it arrives, so filtering never delays the first spoken sentence;
  * a fenced code block becomes one " Code block omitted. " and the rest of
    the block is swallowed (an unterminated block at the end of a reply is
    swallowed too);
  * a table row is held until its line is complete, then spoken as
    "cell, cell, cell"; separator rows are dropped.

Only a line that *might* be a fence or a table row ("`", "``", "|...") is
held back until it's decided - that is the only latency this adds.
Emphasis markers, headings and backticks inside text are left alone here:
voice/tts.py's clean_for_speech() already strips those per sentence.
"""
import re
from typing import Iterable, Iterator

CODE_OMITTED = " Code block omitted. "

_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


def _is_fence(line: str) -> bool:
    return line.lstrip().startswith("```")


def _table_row_for_speech(line: str) -> str:
    stripped = line.strip()
    if "-" in stripped and _TABLE_SEPARATOR_RE.match(stripped):
        return ""
    cells = [c.strip() for c in stripped.strip("|").split("|")]
    cells = [c for c in cells if c]
    return ", ".join(cells)


class SpeechFilter:
    """Incremental markdown-to-speech filter - see the module docstring."""

    def __init__(self):
        self._line = ""          # the current line so far (not including '\n')
        self._emitted = 0        # how many chars of _line were already passed through
        self._in_code = False
        self._line_is_text = False

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        out = []
        for part in re.split(r"(\n)", chunk):
            if part == "\n":
                out.append(self._end_line())
            elif part:
                self._line += part
                out.append(self._pass_through_if_text())
        return "".join(out)

    def flush(self) -> str:
        """End of the reply: decides whatever line is still pending."""
        if not self._line:
            return ""
        return self._end_line(final=True)

    # ------------------------------------------------------------------
    def _pass_through_if_text(self) -> str:
        if self._in_code:
            return ""
        if not self._line_is_text:
            head = self._line.lstrip()
            if not head:
                return ""                        # only indentation so far - undecided
            if head.startswith("|"):
                return ""                        # a table row: wait for the whole line
            if head.startswith("```") or (head.startswith("`") and head.strip("`") == ""):
                return ""                        # a fence, or "`"/"``" that may still become one
            self._line_is_text = True
        piece = self._line[self._emitted:]
        self._emitted = len(self._line)
        return piece

    def _end_line(self, final: bool = False) -> str:
        line, emitted, was_text = self._line, self._emitted, self._line_is_text
        self._line, self._emitted, self._line_is_text = "", 0, False
        newline = "" if final else "\n"

        if self._in_code:
            if _is_fence(line):
                self._in_code = False
            return ""
        if was_text:
            return line[emitted:] + newline
        if _is_fence(line):
            self._in_code = True
            return CODE_OMITTED
        if line.lstrip().startswith("|"):
            spoken = _table_row_for_speech(line)
            return (spoken + newline) if spoken else ""
        return line + newline


def filter_stream(chunks: Iterable[str]) -> Iterator[str]:
    """Generator form for voice/tts.py's speak_stream(): yields only the
    speakable text, as early as it can be decided."""
    f = SpeechFilter()
    for chunk in chunks:
        out = f.feed(chunk or "")
        if out:
            yield out
    tail = f.flush()
    if tail:
        yield tail


def for_speech(text: str) -> str:
    """One-shot form for a finished reply (the chat bubble's replay button,
    non-streamed replies)."""
    f = SpeechFilter()
    return f.feed(text or "") + f.flush()
