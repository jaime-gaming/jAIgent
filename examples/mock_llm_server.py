"""A tiny OpenAI-compatible server for trying jaigent without spending tokens.

It ignores the model's actual intelligence and just replays a fixed plan:
read a file, write a summary, then answer. Useful for demos, for debugging the
agent loop, and for checking your setup before pointing jaigent at a real API.

Run it::

    python examples/mock_llm_server.py            # listens on :8000

Then, in another shell::

    JAIGENT_BASE_URL=http://localhost:8000/v1 \
    JAIGENT_API_KEY=not-a-real-key \
    jaigent "summarise notes.md into summary.md" --verbose
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

# Each element is one assistant turn, replayed in order per conversation length.
PLAN = [
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
    {
        "content": "Done. I listed the workspace and wrote **summary.md**.",
        "tool_calls": None,
    },
]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages", [])

        # Advance through the plan based on how many tool results came back.
        turn = sum(1 for m in messages if m.get("role") == "tool")
        message = PLAN[min(turn, len(PLAN) - 1)]

        payload = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": body.get("model", "mock"),
            "choices": [{"index": 0, "message": {"role": "assistant", **message}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        raw = json.dumps(payload).encode()

        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

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
