"""Offline probe: create, seed, close and resume a disposable Codex thread.

All Codex state belongs to a new temporary directory. The model is a local mock.
The temporary state is kept for inspection and its location is printed.
"""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from codex_context_compacto.rpc import AppServer
from mock_provider import mock_provider


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="codex")
    args = parser.parse_args()
    test_root = Path(tempfile.mkdtemp(prefix="compacto-probe-"))
    child_env = dict(os.environ)
    child_env["CODEX_HOME"] = str(test_root / "codex-state")
    (test_root / "codex-state").mkdir()
    workspace = test_root / "workspace"
    workspace.mkdir()
    config = {
        "model_provider": "compacto-test", "model": "compacto-test",
        "model_providers.compacto-test": {"name": "Offline probe", "base_url": "http://127.0.0.1:9/v1", "wire_api": "responses"},
        "features.plugins": False, "features.apps": False, "features.memories": False,
        "check_for_update_on_startup": False,
    }
    items = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "COMPACTO_HEAD_SENTINEL"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "COMPACTO_MIDDLE_SUMMARY"}]},
        {"type": "function_call", "call_id": "compacto_probe_call", "name": "offline_probe", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "compacto_probe_call", "output": "COMPACTO_TOOL_SENTINEL"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "COMPACTO_TAIL_SENTINEL"}]},
    ]
    print(f"Probe state: {test_root}", flush=True)
    with AppServer(args.codex, config=config, env=child_env) as server:
        result = server.request("thread/start", {"cwd": str(workspace), "sandbox": "read-only", "approvalPolicy": "on-request"})
        thread_id = result["thread"]["id"]
        print(f"Created disposable thread: {thread_id}", flush=True)
        server.request("thread/inject_items", {"threadId": thread_id, "items": items})
        print("Injected five synthetic items", flush=True)
        read = server.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        print(json.dumps(read, ensure_ascii=False), flush=True)
    with mock_provider() as (url, requests):
        config["model_providers.compacto-test"]["base_url"] = url
        config.update({"web_search": "disabled", "features.shell_tool": False,
                       "features.unified_exec": False, "features.multi_agent": False})
        with AppServer(args.codex, config=config, env=child_env) as server:
            resumed = server.request("thread/resume", {"threadId": thread_id})
            print(f"Cold resume succeeded: {resumed['thread']['id']}", flush=True)
            server.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "Reply with the offline test response.", "text_elements": []}]})
            completed = server.wait_notification("turn/completed", timeout=45)
            assert completed["turn"]["status"] == "completed", completed["turn"]
        assert requests, "The local mock did not receive a request."
        rendered = json.dumps(requests[0].get("input"))
        for sentinel in ("COMPACTO_HEAD_SENTINEL", "COMPACTO_MIDDLE_SUMMARY", "COMPACTO_TOOL_SENTINEL", "COMPACTO_TAIL_SENTINEL"):
            assert sentinel in rendered, f"Missing from model input: {sentinel}"
        assert rendered.count("compacto_probe_call") == 2, "Tool call/result pair was not preserved."
        print("PASS: cold-resumed model input contains all four sentinels and the complete tool pair.")
        print("Advertised tools with shell features disabled: " + json.dumps([tool.get("name", tool.get("type")) for tool in requests[0].get("tools", [])]))


if __name__ == "__main__":
    main()
