"""A tiny in-process OpenAI-compatible server (stdlib only) for provider tests.

    srv = FakeOpenAI(); srv.start()
    srv.script = [Reply(200, completion("hello"))]      # consumed one per request
    ... srv.requests[0].json["messages"] ...
"""
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Deque, Dict, List, Optional


@dataclass
class Reply:
    status: int = 200
    body: Any = None
    headers: Dict[str, str] = field(default_factory=dict)
    delay: float = 0.0


@dataclass
class Seen:
    method: str
    path: str
    headers: Dict[str, str]
    json: Optional[dict]


def completion(text=None, tool_calls=None, model="fake-model"):
    msg = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"id": "cmpl-1", "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"total_tokens": 7}}


def tool_call(name, args, id="call_abc"):
    return {"id": id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def error_body(message, param=None, type_="invalid_request_error"):
    return {"error": {"message": message, "type": type_, "param": param, "code": None}}


# --------------------------------------------------------------------------
# Tier 2: Server-Sent-Events helpers, for scripting a streaming
# /chat/completions reply the same way completion()/tool_call() above
# script a one-shot one. Pass the resulting string as a Reply's body
# (FakeOpenAI writes a plain str body as-is - see FakeOpenAI._reply) -
# http.server sends it as one chunk, which is fine: requests.iter_lines()
# splits on line boundaries regardless of how many TCP packets it arrived
# in, so this exercises the real parsing path, just not real network
# trickle-in timing (not something a unit test can usefully fake anyway).
# --------------------------------------------------------------------------
def sse_chunk(delta=None, finish_reason=None, index=0, id="cmpl-1", model="fake-model"):
    """One `data: {...}` SSE line (OpenAI/Groq streaming chunk shape)."""
    choice = {"index": index, "delta": delta or {}, "finish_reason": finish_reason}
    payload = {"id": id, "model": model, "choices": [choice]}
    return f"data: {json.dumps(payload)}\n\n"


def sse_error(message, type_="server_error"):
    """A mid-stream error event - a provider that started a 200 stream and
    then fails partway through, rather than rejecting the request up
    front (that case is a plain non-200 Reply, not this)."""
    return f"data: {json.dumps(error_body(message, type_=type_))}\n\n"


def sse_tool_call_delta(index, id=None, name=None, arguments=None, type_="function"):
    """One entry of a delta's tool_calls list - every field but `index` is
    optional because real providers split a single tool call's id/name/
    arguments across several chunks."""
    d = {"index": index}
    if id is not None:
        d["id"] = id
    if type_ is not None:
        d["type"] = type_
    fn = {}
    if name is not None:
        fn["name"] = name
    if arguments is not None:
        fn["arguments"] = arguments
    if fn:
        d["function"] = fn
    return d


def sse_body(*chunks: str) -> str:
    """Joins sse_chunk(...)/sse_error(...) strings and appends the
    terminating [DONE] event - the full string to use as a streaming
    Reply's body."""
    return "".join(chunks) + "data: [DONE]\n\n"


class FakeOpenAI:
    @property
    def script(self) -> Deque[Reply]:
        return self._script

    @script.setter
    def script(self, replies):
        self._script = deque(replies)   # tests may assign a plain list

    def __init__(self):
        self._script: Deque[Reply] = deque()
        self.requests: List[Seen] = []
        self.default = Reply(200, completion("default reply"))
        self.get_routes: Dict[str, Reply] = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _reply(self, r: Reply):
                if r.delay:
                    time.sleep(r.delay)
                raw = json.dumps(r.body).encode() if not isinstance(r.body, (bytes, str)) else \
                    (r.body if isinstance(r.body, bytes) else r.body.encode())
                self.send_response(r.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in r.headers.items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                try:
                    parsed = json.loads(body) if body else None
                except ValueError:
                    parsed = None
                outer.requests.append(Seen("POST", self.path, {k.lower(): v for k, v in self.headers.items()}, parsed))
                self._reply(outer.script.popleft() if outer.script else outer.default)

            def do_GET(self):
                outer.requests.append(Seen("GET", self.path, {k.lower(): v for k, v in self.headers.items()}, None))
                self._reply(outer.get_routes.get(self.path, Reply(404, error_body("no such route"))))

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._srv.server_address[1]}/v1"

    def posts(self) -> List[Seen]:
        return [r for r in self.requests if r.method == "POST"]
