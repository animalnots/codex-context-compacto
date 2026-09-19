"""Local Responses endpoint for offline compatibility tests; no API credentials."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading


@contextmanager
def mock_provider():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(json.loads(body))
            message = {"type": "message", "id": "msg_compacto_mock", "role": "assistant",
                       "status": "completed", "content": [{"type": "output_text", "text": "Offline summary: preserve the requested task, decisions and next steps.", "annotations": []}]}
            response = {"id": "resp_compacto_mock", "object": "response", "created_at": 1,
                        "status": "completed", "model": "compacto-test", "output": [message],
                        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                                  "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}
            if not requests[-1].get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())
                return
            events = [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": {**message, "status": "in_progress", "content": []}},
                {"type": "response.content_part.added", "item_id": message["id"], "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}},
                {"type": "response.output_text.delta", "item_id": message["id"], "output_index": 0, "content_index": 0, "delta": message["content"][0]["text"]},
                {"type": "response.output_text.done", "item_id": message["id"], "output_index": 0, "content_index": 0, "text": message["content"][0]["text"]},
                {"type": "response.output_item.done", "output_index": 0, "item": message},
                {"type": "response.completed", "response": response},
            ]
            data = "".join("event: " + event["type"] + "\ndata: " + json.dumps({**event, "sequence_number": index}) + "\n\n" for index, event in enumerate(events)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
