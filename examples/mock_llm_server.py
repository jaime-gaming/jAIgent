"""A tiny OpenAI-compatible server for trying jaigent without spending tokens.

It ignores the model's actual intelligence and just replays a fixed plan: list
the workspace, write a summary, then answer. Both streaming and non-streaming
requests are supported, so it exercises the same code paths a real provider does.

Run it::

    python examples/mock_llm_server.py            # listens on :8000

Then, in another shell::

    JAIGENT_BASE_URL=http://localhost:8000/v1 \
    JAIGENT_API_KEY=not-a-real-key \
    jaigent "summarise notes.md into summary.md" --verbose
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

FINAL_TEXT = (
    "Done. I listed the workspace and wrote **summary.md** with a short overview "
    "of what is in this folder."
)

# Each element is one assistant turn, replayed in order.
PLAN: list[dict] = [
    {
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "list_files", "arguments": json.dumps({"path": "."})},
            }
        ],
    },
    {
        "content": None,
        "tool_calls": [
            {
                "id": "call_2",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps(
                        {
                            "path": "summary.md",
                            "content": "# Summary\n\nWritten by jaigent via the mock server.\n",
                        }
                    ),
                },
            }
        ],
    },
    {"content": FINAL_TEXT, "tool_calls": None},
]

USAGE = {"prompt_tokens": 820, "completion_tokens": 145, "total_tokens": 965}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages", [])

        # Advance through the plan based on how many tool results came back.
        turn = sum(1 for m in messages if m.get("role") == "tool")
        message = PLAN[min(turn, len(PLAN) - 1)]

        if body.get("stream"):
            self._stream(message, body.get("model", "mock"))
        else:
            self._once(message, body.get("model", "mock"))

    # ------------------------------------------------------------------
    def _once(self, message: dict, model: str) -> None:
        payload = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", **message}}],
            "usage": USAGE,
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _stream(self, message: dict, model: str) -> None:
        """Emit the reply as server-sent events, a few characters at a time."""
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()

        def send(delta: dict) -> None:
            event = {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": delta}],
            }
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()

        if message.get("tool_calls"):
            for index, call in enumerate(message["tool_calls"]):
                send(
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": call["id"],
                                "type": "function",
                                "function": {"name": call["function"]["name"], "arguments": ""},
                            }
                        ]
                    }
                )
                # Arguments arrive in fragments, as with a real provider.
                arguments = call["function"]["arguments"]
                for start in range(0, len(arguments), 16):
                    send(
                        {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "function": {"arguments": arguments[start : start + 16]},
                                }
                            ]
                        }
                    )
        elif message.get("content"):
            for start in range(0, len(message["content"]), 5):
                send({"content": message["content"][start : start + 5]})
                time.sleep(0.02)  # visible typing effect in the demo

        usage_event = {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [],
            "usage": USAGE,
        }
        self.wfile.write(f"data: {json.dumps(usage_event)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *args: object) -> None:
        return  # keep the demo output clean


def main(port: int = 8000) -> None:
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Mock OpenAI-compatible server on http://localhost:{port}/v1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
