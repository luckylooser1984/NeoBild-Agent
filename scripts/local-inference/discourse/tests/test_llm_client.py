import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.llm_client import chat_completion, THINK_PREFILL


class _Handler(BaseHTTPRequestHandler):
    response_body = b"{}"
    status_code = 200
    last_request_body = b""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _Handler.last_request_body = self.rfile.read(length)
        self.send_response(_Handler.status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(_Handler.response_body)

    def log_message(self, *args):
        pass


def _serve(response_json, status_code=200):
    _Handler.response_body = json.dumps(response_json).encode("utf-8")
    _Handler.status_code = status_code
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# Real response shapes captured from llama-server (0.4.x-dev) with
# Qwen3-1.7B-Q8_0.
REAL_PREFILL_RESPONSE = {
    "choices": [
        {
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": THINK_PREFILL + "2 + 2 = 4."},
        }
    ],
    "usage": {"prompt_tokens": 30, "completion_tokens": 9, "total_tokens": 39},
    "timings": {"predicted_per_second": 14.855171363686162},
}

REAL_EMPTY_ON_LENGTH_RESPONSE = {
    "choices": [
        {
            "finish_reason": "length",
            "message": {"role": "assistant", "content": "", "reasoning_content": "Okay, thinking..."},
        }
    ],
    "usage": {"prompt_tokens": 22, "completion_tokens": 40, "total_tokens": 62},
    "timings": {"predicted_per_second": 13.366747918214696},
}


class ChatCompletionTests(unittest.TestCase):
    def test_strips_prefill_echo_from_content(self):
        server = _serve(REAL_PREFILL_RESPONSE)
        try:
            base_url = f"http://127.0.0.1:{server.server_port}/v1"
            result = chat_completion(
                base_url,
                [{"role": "user", "content": "Say in one sentence what 2+2 is."}],
                model="qwen3-1.7b", temperature=0.7, seed=42, max_tokens=60,
            )
        finally:
            server.shutdown()
        self.assertEqual(result.content, "2 + 2 = 4.")
        self.assertEqual(result.raw_content, THINK_PREFILL + "2 + 2 = 4.")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.completion_tokens, 9)
        self.assertAlmostEqual(result.tokens_per_second, 14.855171363686162)
        self.assertIsNone(result.error)

    def test_appends_prefill_message_to_request(self):
        server = _serve(REAL_PREFILL_RESPONSE)
        try:
            base_url = f"http://127.0.0.1:{server.server_port}/v1"
            chat_completion(
                base_url, [{"role": "user", "content": "x"}],
                model="qwen3-1.7b", temperature=0.7, seed=42, max_tokens=60,
            )
            sent = json.loads(_Handler.last_request_body)
        finally:
            server.shutdown()
        self.assertEqual(sent["messages"][-1], {"role": "assistant", "content": THINK_PREFILL})

    def test_surfaces_empty_content_on_length_without_crashing(self):
        server = _serve(REAL_EMPTY_ON_LENGTH_RESPONSE)
        try:
            base_url = f"http://127.0.0.1:{server.server_port}/v1"
            result = chat_completion(
                base_url, [{"role": "user", "content": "x"}],
                model="qwen3-1.7b", temperature=0.7, seed=42, max_tokens=40,
            )
        finally:
            server.shutdown()
        self.assertEqual(result.content, "")
        self.assertEqual(result.finish_reason, "length")

    def test_connection_error_returns_error_field_not_exception(self):
        result = chat_completion(
            "http://127.0.0.1:1/v1", [{"role": "user", "content": "x"}],
            model="qwen3-1.7b", temperature=0.7, seed=42, max_tokens=40, timeout_s=2.0,
        )
        self.assertIsNotNone(result.error)
        self.assertEqual(result.finish_reason, "error")


if __name__ == "__main__":
    unittest.main()
