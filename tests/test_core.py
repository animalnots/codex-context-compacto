import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_context_compacto.cli import main
from codex_context_compacto.config import Config, load_config
from codex_context_compacto.hook import handle
from codex_context_compacto.planner import flatten, make_plan, seed_items
from codex_context_compacto.rpc import AppServer, CompactionError, toml_value
from codex_context_compacto.service import apply_job, load_job, prepare
from codex_context_compacto.storage import job_lock, read_json, write_json
from codex_context_compacto.summarizer import NoRedirect, contains_media, summarize
from codex_context_compacto.transcript import clean_item, estimate_tokens, group_items, locate_session, read_snapshot, verify_source
from mock_provider import mock_provider


def message(role, text):
    return {"type": "message", "role": role, "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}


def fixture_items():
    return [
        message("user", "HEAD_SENTINEL: build a parser"), message("assistant", "I will preserve the original source."),
        message("user", "MIDDLE_RAW_SENTINEL " + "An old discussion about parsing decisions. " * 100),
        message("assistant", "Old implementation details and test results. " * 100),
        message("user", "TAIL_USER_SENTINEL: continue the tests"),
        {"type": "function_call", "name": "exec_command", "call_id": "retained_call", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "retained_call", "output": "TAIL_TOOL_SENTINEL: 12 tests passed"},
        message("assistant", "TAIL_ASSISTANT_SENTINEL: next test unicode"),
    ]


def write_rollout(path, cwd, items=None, *, identifier=None, extra=None):
    identifier = identifier or str(uuid4())
    records = [
        {"type": "session_meta", "payload": {"id": identifier, "cwd": str(cwd), "model_provider": "compacto-test"}},
        {"type": "turn_context", "payload": {"cwd": str(cwd), "model": "compacto-test", "sandbox_policy": {"type": "read-only"}}},
        *({"type": "response_item", "payload": item} for item in (fixture_items() if items is None else items)),
        *(extra or []),
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return identifier


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="compacto-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.jsonl"
        self.identifier = write_rollout(self.source, self.root)
        self.config = Config(head_tokens=300, tail_tokens=300, summary_tokens=400)
        self.snapshot = read_snapshot(self.source)

    def prepare(self):
        return prepare(self.snapshot, self.config, self.root / "state")

    def test_boundaries_and_tool_pair(self):
        plan = make_plan(self.snapshot, self.config)
        self.assertEqual(plan.head, fixture_items()[:2])
        self.assertEqual(plan.middle, fixture_items()[2:4])
        self.assertEqual(plan.tail, fixture_items()[4:])
        seeded = seed_items(plan, "Parsing decisions, completed changes and next steps.", self.config, self.identifier)
        self.assertEqual(len(seeded), 7)
        self.assertEqual(seeded[2]["role"], "user")
        self.assertNotIn("MIDDLE_RAW_SENTINEL", json.dumps(seeded))
        self.assertEqual(seeded[3:], plan.tail)

    def test_original_never_modified_by_prepare(self):
        before = self.source.read_bytes()
        job, _ = self.prepare()
        self.assertEqual(self.source.read_bytes(), before)
        self.assertTrue((job / "preview.md").is_file())
        self.assertEqual(read_json(job / "middle.json"), fixture_items()[2:4])

    def test_zero_head_budget(self):
        plan = make_plan(self.snapshot, self.config.updated(head_tokens=0))
        self.assertEqual(plan.head, [])

    def test_zero_tail_budget(self):
        plan = make_plan(self.snapshot, self.config.updated(tail_tokens=0))
        self.assertEqual(plan.tail, [])

    def test_no_middle_no_new_session(self):
        config = self.config.updated(tail_tokens=25000)
        with self.assertRaisesRegex(CompactionError, "Nothing"):
            prepare(self.snapshot, config, self.root / "state")

    def test_oversized_tail_is_not_truncated(self):
        with self.assertRaisesRegex(CompactionError, "Last complete group"):
            make_plan(self.snapshot, self.config.updated(tail_tokens=1))

    def test_oversized_head_is_not_truncated(self):
        with self.assertRaisesRegex(CompactionError, "First complete group"):
            make_plan(self.snapshot, self.config.updated(head_tokens=1))

    def test_summary_empty_and_over_budget(self):
        plan = make_plan(self.snapshot, self.config)
        for text in ("", "\x00", "x" * 2000):
            with self.subTest(text=text[:10]), self.assertRaises(CompactionError):
                seed_items(plan, text, self.config, self.identifier)

    def test_no_expansion_disguised_as_compaction(self):
        plan = make_plan(self.snapshot, self.config)
        plan.stats["input_estimated_tokens"] = 1
        with self.assertRaisesRegex(CompactionError, "not smaller"):
            seed_items(plan, "A summary", self.config, self.identifier)

    def test_unicode_estimator_uses_bytes(self):
        self.assertEqual(estimate_tokens("abc"), 1)
        self.assertEqual(estimate_tokens("\U0001f600"), 2)

    def test_dangling_result_rejected(self):
        with self.assertRaisesRegex(CompactionError, "missing its matching call"):
            group_items([fixture_items()[6]])

    def test_dangling_call_requires_explicit_omission(self):
        write_rollout(self.source, self.root, fixture_items()[:6])
        with self.assertRaisesRegex(CompactionError, "unfinished tool"):
            read_snapshot(self.source)
        partial = read_snapshot(self.source, completed_only=True)
        self.assertEqual(flatten(partial.groups), fixture_items()[:4])
        self.assertTrue(any("omitted" in warning for warning in partial.warnings))

    def test_parallel_calls_can_complete_out_of_order(self):
        first = fixture_items()[5]
        second = {**first, "call_id": "second"}
        output = fixture_items()[6]
        items = [message("user", "run two tools"), first, second, {**output, "call_id": "second"}, output]
        self.assertEqual(flatten(group_items(items)[0]), items)

    def test_custom_tool_pair(self):
        items = [message("user", "patch"), {"type": "custom_tool_call", "call_id": "patch", "name": "apply_patch", "input": "test"},
                 {"type": "custom_tool_call_output", "call_id": "patch", "output": "ok"}]
        self.assertEqual(flatten(group_items(items)[0]), items)

    def test_tool_pair_cannot_cross_user_boundary(self):
        with self.assertRaisesRegex(CompactionError, "splits an unfinished"):
            group_items([fixture_items()[5], message("user", "next"), fixture_items()[6]])

    def test_duplicate_call_id_rejected(self):
        with self.assertRaisesRegex(CompactionError, "duplicate"):
            group_items([fixture_items()[5], fixture_items()[5]])

    def test_source_metadata_and_reasoning_not_replayed(self):
        items = [message("developer", "old privileged instructions"), {"type": "reasoning", "encrypted_content": "opaque"},
                 {**message("user", "actual input"), "id": "source_item", "internal_chat_message_metadata_passthrough": {"turn_id": "old"}}]
        write_rollout(self.source, self.root, items)
        parsed = read_snapshot(self.source)
        self.assertEqual(flatten(parsed.groups), [message("user", "actual input")])
        self.assertTrue(parsed.warnings)

    def test_unknown_and_opaque_compaction_items_rejected(self):
        for kind in ("other", "context_compaction", "compaction", "agent_message"):
            with self.subTest(kind=kind), self.assertRaises(CompactionError):
                clean_item({"type": kind}, [])

    def test_native_replacement_replaces_old_history(self):
        replacement = [message("user", "prior native summary"), message("assistant", "retained")]
        write_rollout(self.source, self.root, extra=[{"type": "compacted", "payload": {"replacement_history": replacement}}])
        self.assertEqual(flatten(read_snapshot(self.source).groups), replacement)

    def test_ambiguous_native_compaction_rejected(self):
        write_rollout(self.source, self.root, extra=[{"type": "compacted", "payload": {"message": "summary only"}}])
        with self.assertRaisesRegex(CompactionError, "replacement_history"):
            read_snapshot(self.source)

    def test_rollback_rejected(self):
        write_rollout(self.source, self.root, extra=[{"type": "event_msg", "payload": {"type": "thread_rolled_back", "num_turns": 1}}])
        with self.assertRaisesRegex(CompactionError, "Rollbacks"):
            read_snapshot(self.source)

    def test_partial_jsonl_rejected(self):
        with self.source.open("a", encoding="utf-8") as stream:
            stream.write('{"type":')
        with self.assertRaisesRegex(CompactionError, "partially written"):
            read_snapshot(self.source)

    def test_append_requires_opt_in(self):
        with self.source.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "response_item", "payload": message("user", "later")}) + "\n")
        with self.assertRaisesRegex(CompactionError, "grew"):
            verify_source(self.source, self.snapshot.digest, self.snapshot.size)
        self.assertTrue(verify_source(self.source, self.snapshot.digest, self.snapshot.size, allow_appended=True))

    def test_rewrite_rejected_even_with_append_opt_in(self):
        self.source.write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(CompactionError, "rewritten"):
            verify_source(self.source, self.snapshot.digest, self.snapshot.size, allow_appended=True)

    def test_tampered_prepared_items_rejected(self):
        job, _ = self.prepare()
        bundle = read_json(job / "plan.json")
        bundle["head"][0]["role"] = "developer"
        write_json(job / "plan.json", bundle)
        with self.assertRaisesRegex(CompactionError, "edited"):
            load_job(job)

    def test_stable_hook_job_is_deduplicated(self):
        first, _ = prepare(self.snapshot, self.config, self.root, stable=True)
        second, _ = prepare(self.snapshot, self.config, self.root, stable=True)
        self.assertEqual(first, second)

    def test_exclusive_job_lock(self):
        job, _ = self.prepare()
        with job_lock(job):
            with self.assertRaisesRegex(CompactionError, "locked"):
                with job_lock(job):
                    self.fail("second process acquired lock")
        self.assertFalse((job / ".apply.lock").exists())

    def test_previous_partial_attempt_never_repeated(self):
        job, _ = self.prepare()
        write_json(job / "receipt.json", {"status": "seeded", "thread_id": str(uuid4())})
        with self.assertRaisesRegex(CompactionError, "no duplicate"):
            apply_job(job, summary="checkpoint")

    def test_verified_receipt_is_reused_without_api(self):
        job, _ = self.prepare()
        identifier = str(uuid4())
        write_json(job / "receipt.json", {"status": "verified", "thread_id": identifier})
        with patch("codex_context_compacto.service.summarize", side_effect=AssertionError):
            result = apply_job(job)
        self.assertTrue(result["reused"])
        self.assertEqual(result["thread_id"], identifier)

    def test_strict_config(self):
        for values in ({"enabled": "yes"}, {"head_tokens": True}, {"tail_tokens": -1}, {"unknown": 1},
                       {"max_output_tokens": 1}, {"backend": "shell"}, {"enabled": True},
                       {"api_base_url": "http://example.com"}, {"api_base_url": "https://user:key@example.com/v1"},
                       {"timeout_seconds": 500}):
            with self.subTest(values=values), self.assertRaises(CompactionError):
                Config().updated(**values)

    def test_config_does_not_store_secrets(self):
        self.assertNotIn("api_key", Config().to_dict())
        self.assertEqual(load_config(self.root).backend, "manual")

    def test_session_selector_rejects_glob(self):
        with self.assertRaisesRegex(CompactionError, "UUID"):
            locate_session("*")

    def test_missing_session_id_is_a_handled_error(self):
        from codex_context_compacto.transcript import session_uuid
        with self.assertRaises(CompactionError):
            session_uuid(None)

    def test_preview_has_no_writes(self):
        root = self.root / "uncreated-state"
        with contextlib.redirect_stdout(io.StringIO()) as output:
            status = main(["preview", "--transcript", str(self.source), "--state-dir", str(root), "--head-tokens", "300", "--tail-tokens", "300"])
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(output.getvalue())["would_compact"])
        self.assertFalse(root.exists())

    def test_disabled_hook_has_no_side_effects(self):
        state = self.root / "uncreated-hook-state"
        self.assertEqual(handle({}, root=state), {})
        self.assertFalse(state.exists())

    def test_hook_session_mismatch_blocks_before_api(self):
        config = self.config.updated(backend="responses", summary_model="mock", enabled=True)
        write_json(self.root / "config.json", config.to_dict())
        result = handle({"hook_event_name": "PreCompact", "session_id": str(uuid4()), "transcript_path": str(self.source)}, root=self.root)
        self.assertFalse(result["continue"])
        self.assertIn("does not match", result["stopReason"])

    def test_hook_uses_codex_not_claude_contract(self):
        config = self.config.updated(backend="responses", summary_model="mock", enabled=True)
        write_json(self.root / "config.json", config.to_dict())
        payload = {"hook_event_name": "PreCompact", "session_id": self.identifier, "transcript_path": str(self.source)}
        with patch("codex_context_compacto.hook.apply_job", return_value={"resume_command": "codex resume test"}):
            result = handle(payload, root=self.root)
        self.assertFalse(result["continue"])
        self.assertNotIn("decision", result)
        self.assertIn("codex resume", result["stopReason"])

    def test_responses_backend_is_tool_free_and_explicit(self):
        plan = make_plan(self.snapshot, self.config)
        with mock_provider() as (url, requests), patch.dict(os.environ, {"COMPACTO_TEST_KEY": "synthetic-test-key"}):
            config = self.config.updated(backend="responses", summary_model="compacto-test", api_key_env="COMPACTO_TEST_KEY", api_base_url=url)
            result = summarize(plan, config)
        self.assertIn("Offline summary", result)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["tools"], [])
        self.assertEqual(requests[0]["tool_choice"], "none")
        self.assertFalse(requests[0]["store"])
        self.assertIn("MIDDLE_RAW_SENTINEL", json.dumps(requests[0]["input"]))
        self.assertNotIn("TAIL_TOOL_SENTINEL", json.dumps(requests[0]["input"]))

    def test_manual_backend_does_not_call_network(self):
        with self.assertRaisesRegex(CompactionError, "No automatic backend"):
            summarize(make_plan(self.snapshot, self.config), self.config)

    def test_api_key_missing_is_actionable(self):
        config = self.config.updated(backend="responses", summary_model="mock", api_key_env="COMPACTO_NONEXISTENT_TEST_KEY")
        with self.assertRaisesRegex(CompactionError, "subscription login"):
            summarize(make_plan(self.snapshot, config), config)

    def test_media_is_detected(self):
        self.assertTrue(contains_media({"content": [{"type": "input_image", "image_url": "data:..."}]}))
        self.assertFalse(contains_media(fixture_items()))

    def test_redirect_never_forwards_credentials(self):
        with self.assertRaisesRegex(CompactionError, "redirected"):
            NoRedirect().redirect_request(None, None, 307, "", {}, "https://elsewhere.example")

    def test_api_failures_and_refusals_do_not_become_checkpoints(self):
        config = self.config.updated(backend="responses", summary_model="mock", api_key_env="COMPACTO_TEST_KEY")
        plan = make_plan(self.snapshot, config)
        responses = [
            {"status": "incomplete", "output": []},
            {"status": "completed", "output": []},
            {"status": "completed", "output": [{"type": "function_call", "name": "do_not_execute"}]},
            {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": "no"}]}]},
            {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "x" * 2000}]}]},
        ]
        for value in responses:
            opener = SimpleNamespace(open=lambda *args, **kwargs: io.BytesIO(json.dumps(value).encode()))
            with self.subTest(value=str(value)[:80]), patch.dict(os.environ, {"COMPACTO_TEST_KEY": "synthetic"}), patch("codex_context_compacto.summarizer.request.build_opener", return_value=opener), self.assertRaises(CompactionError):
                summarize(plan, config)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.snapshot.digest)

    def test_network_failures_are_bounded_and_do_not_leak_error_bodies(self):
        config = self.config.updated(backend="responses", summary_model="mock", api_key_env="COMPACTO_TEST_KEY")
        for failure in (HTTPError("https://test", 429, "sensitive-body-sentinel", {}, None), URLError("sensitive-body-sentinel"), TimeoutError("sensitive-body-sentinel")):
            with self.subTest(failure=type(failure).__name__), patch.dict(os.environ, {"COMPACTO_TEST_KEY": "synthetic"}), patch("codex_context_compacto.summarizer.request.build_opener") as opener:
                opener.return_value.open.side_effect = failure
                with self.assertRaises(CompactionError) as caught:
                    summarize(make_plan(self.snapshot, config), config)
                self.assertNotIn("sensitive-body-sentinel", str(caught.exception))
                self.assertEqual(opener.return_value.open.call_count, 1)

    def test_source_growth_during_summary_prevents_thread_creation(self):
        job, _ = self.prepare()

        def slow_summary(*_):
            with self.source.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "response_item", "payload": message("user", "new work")}) + "\n")
            return "A short checkpoint"

        with patch("codex_context_compacto.service.summarize", side_effect=slow_summary), self.assertRaisesRegex(CompactionError, "grew"):
            apply_job(job)
        self.assertFalse((job / "receipt.json").exists())

    def test_failed_injection_records_thread_and_does_not_retry(self):
        job, _ = self.prepare()
        thread_id = str(uuid4())

        class FailingServer:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def request(self, method, params):
                if method == "thread/start":
                    return {"thread": {"id": thread_id, "path": str(job / "disposable.jsonl")}}
                raise CompactionError("Simulated injection failure")

        with self.assertRaisesRegex(CompactionError, "injection failure"):
            apply_job(job, summary="Reviewed checkpoint", server_factory=FailingServer)
        receipt = read_json(job / "receipt.json")
        self.assertEqual(receipt["thread_id"], thread_id)
        self.assertEqual(receipt["status"], "created")
        with self.assertRaisesRegex(CompactionError, "no duplicate"):
            apply_job(job, summary="Reviewed checkpoint", server_factory=FailingServer)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.snapshot.digest)

    def test_app_server_never_approves_server_requests(self):
        server = AppServer(timeout=0.1)
        output = io.StringIO()
        server.process = SimpleNamespace(stdin=output)
        server.messages.put({"id": "approval", "method": "item/commandExecution/requestApproval", "params": {}})
        server.messages.put({"id": 1, "result": {"ok": True}})
        self.assertEqual(server.request("synthetic-test", {}), {"ok": True})
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(replies[1]["id"], "approval")
        self.assertIn("error", replies[1])

    def test_app_server_timeout_is_actionable(self):
        server = AppServer(timeout=0.01)
        server.process = SimpleNamespace(stdin=io.StringIO())
        with self.assertRaisesRegex(CompactionError, "timed out"):
            server.request("synthetic-test", {})

    def test_toml_override_preserves_windows_paths(self):
        value = {"name": "test", "path": "C:\\space here\\folder", "enabled": False}
        import tomllib
        self.assertEqual(tomllib.loads("x = " + toml_value(value))["x"], value)


@unittest.skipUnless(os.environ.get("COMPACTO_TEST_CODEX") == "1", "set COMPACTO_TEST_CODEX=1 for the real CLI / local mock integration test")
class CodexIntegrationTests(unittest.TestCase):
    def test_full_compaction_cold_resume_and_model_input(self):
        with tempfile.TemporaryDirectory(prefix="compacto-integration-") as directory, mock_provider() as (url, requests):
            root = Path(directory)
            workspace, state = root / "workspace", root / "codex-state"
            workspace.mkdir()
            state.mkdir()
            source = root / "source.jsonl"
            write_rollout(source, workspace)
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            config = Config(head_tokens=300, tail_tokens=300, summary_tokens=400,
                            backend="responses", summary_model="compacto-test", api_key_env="COMPACTO_TEST_KEY", api_base_url=url)
            child_env = {**os.environ, "CODEX_HOME": str(state)}
            overrides = {"model": "compacto-test", "model_provider": "compacto-test",
                         "model_providers.compacto-test": {"name": "Offline integration", "base_url": url, "wire_api": "responses"},
                         "features.plugins": False, "features.apps": False, "features.memories": False,
                         "web_search": "disabled", "check_for_update_on_startup": False}

            def factory(executable, **kwargs):
                return AppServer(executable, env=child_env, config=overrides, **kwargs)

            snapshot = read_snapshot(source)
            job, _ = prepare(snapshot, config, root / "plugin-state")
            with patch.dict(os.environ, {"COMPACTO_TEST_KEY": "synthetic-test-key"}):
                try:
                    result = apply_job(job, server_factory=factory)
                except CompactionError:
                    receipt = read_json(job / "receipt.json")
                    if receipt.get("transcript"):
                        actual = flatten(read_snapshot(Path(receipt["transcript"])).groups)
                        expected = seed_items(make_plan(snapshot, config), (job / "summary.md").read_text(encoding="utf-8"), config, snapshot.session_id)
                        self.assertEqual(actual[-len(expected):], expected, "Persisted synthetic test fixture differs from seed")
                    raise
            self.assertEqual(result["status"], "verified")
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)
            self.assertTrue(apply_job(job)["reused"])
            with factory("codex", timeout=45) as server:
                resumed = server.request("thread/resume", {"threadId": result["thread_id"]})
                self.assertEqual(resumed["thread"]["id"], result["thread_id"])
                server.request("turn/start", {"threadId": result["thread_id"], "input": [{"type": "text", "text": "Return the offline test reply.", "text_elements": []}]})
                completed = server.wait_notification("turn/completed", timeout=45)
                self.assertEqual(completed["turn"]["status"], "completed")
            self.assertGreaterEqual(len(requests), 2)
            actual = json.dumps(requests[-1]["input"])
            for sentinel in ("HEAD_SENTINEL", "TAIL_TOOL_SENTINEL", "TAIL_ASSISTANT_SENTINEL", "Offline summary"):
                self.assertIn(sentinel, actual)
            self.assertNotIn("MIDDLE_RAW_SENTINEL", actual)
            self.assertEqual(actual.count("retained_call"), 2)


if __name__ == "__main__":
    unittest.main()
