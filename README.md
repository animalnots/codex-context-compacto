# Codex Context Compacto

Preserve selected conversation history around a summarized middle, then continue
in a **new Codex CLI session**. The original session is never rewritten or deleted.

```text
source:       [ head ][          middle          ][ tail ]
new session:  [ head ][ reviewed short checkpoint ][ tail ]
```

An independent adaptation of the head/middle/tail idea from
`claude-code-context-compacto-plugin`, not a Claude transcript converter or an
official OpenAI plugin. Version 0.1.0 is an early, deliberately bounded release.

## What works

- Native Windows PowerShell, plus Python support for macOS/Linux. No WSL or tmux required.
- Separate head/tail budgets, with complete conversation groups and matched tool calls/results.
- Read-only previews and private, inspectable preparation artifacts.
- A bundled `compacto` skill: use the current Codex session to write the checkpoint.
- Optional tool-free Responses API summarization and an opt-in `PreCompact` hook.
- A new persisted thread created through `thread/start` and `thread/inject_items`.
- Source fingerprints, per-job locks, summary checks, persistence verification, and retry receipts.

**Not included:** automatic terminal handoff, automatic continuation, in-place
replacement of the running session, or a clone of Claude's command set. After
compaction you run the printed `codex resume UUID` command yourself.

## Requirements

Python **3.11+**, Codex CLI on `PATH`, and an existing Codex login for subsequent
normal work. Tested against **Codex CLI 0.154.0 on native Windows**. The API and
rollout formats can change; run the offline integration test after upgrading.

The helper has **no third-party Python runtime dependencies**. A separately billed
API key is needed only for the optional unattended Responses backend. The manual
skill path uses your current Codex session and does not require a separate key.

## Start from a checkout (PowerShell)

Run these from this repository's root:

```powershell
py -3 scripts/compacto.py doctor
py -3 scripts/compacto.py --help
```

On macOS/Linux, replace `py -3` with `python3`. Optionally install the **CLI helper**
with `py -3 -m pip install .`, then use `compacto` instead of
`py -3 scripts/compacto.py`. The Python wheel is the helper; the Git checkout is
the complete plugin package, including its skill and hook.

### Manual checkpoint: no separate API key

Choose the actual source session ID, never a guessed "latest" session. Substitute
it for `SESSION_UUID`. Alternatively use `--transcript "FULL_PATH_TO_ROLLOUT.jsonl"`.
Rollouts normally live under `CODEX_HOME/sessions` (`~/.codex/sessions` by default).
When launched from a Codex tool, `CODEX_THREAD_ID` can supply the ID automatically.

```powershell
py -3 scripts/compacto.py preview --session SESSION_UUID
py -3 scripts/compacto.py prepare --session SESSION_UUID
```

`preview` does not write files or contact a model. `prepare` writes a private job
and prints its `job`, `preview`, and `middle` paths. Inspect `preview.md`, then
summarize `middle.json` into a UTF-8 `checkpoint.md` in that job. Ask Codex to do
this using the bundled skill, or write/review it yourself. Keep goals, constraints,
decisions, changed files, tests, unresolved problems and next steps; omit secrets.

```powershell
py -3 scripts/compacto.py apply --job "JOB_DIRECTORY" --summary-file "JOB_DIRECTORY/checkpoint.md"
# Run the exact resume_command returned by a verified result:
codex resume NEW_SESSION_UUID
```

The default budgets are head **0**, tail **25,000**, summary **4,000**, with a
**40,000** total seed ceiling. These are estimates, not exact model token counts.
For a different selection, pass `--head-tokens 3000 --tail-tokens 20000` to
`preview`, `prepare`, or `compact`. A job freezes its configuration at preparation.
If all history already fits the retained windows, no replacement is created.

**Best first run:** use an idle source session from a second terminal. Working
inside the source session adds more transcript records while creating the summary:

- `prepare --completed-only` explicitly excludes the final unfinished tool group.
  Its warnings describe the omission. This can exclude an entire working turn;
  inspect it before proceeding.
- `apply --allow-appended` explicitly permits resuming the older snapshot, without
  activity appended since preparation. It still rejects a rewritten/truncated
  source. Do not use it to discard unrelated new work inadvertently.

Neither flag modifies the original, and neither is silently enabled.

## Use it as a Codex plugin

This repository is a plugin root: it contains portable `plugin.json`, the
compatibility `.codex-plugin/plugin.json`, `skills/compacto`, and `hooks/hooks.json`.
The plugin must retain its `src/` directory alongside its scripts.

To register this checkout, ask Codex's built-in **plugin-creator** skill:

> Register the existing codex-context-compacto folder in my personal marketplace.
> Preserve its source files. Install it locally, but leave its automatic hook disabled.

This is a separate, explicit installation step because it changes your personal
marketplace and Codex configuration. The source checkout itself makes no such
changes. In CLI 0.154.0, installation from a registered marketplace uses
`codex plugin add PLUGIN@MARKETPLACE` (not `codex plugin install`). Use the actual
marketplace name returned during registration, then start a new Codex session.

Ask for `$compacto` to prepare a checkpoint. You can also ask Codex to read
`skills/compacto/SKILL.md` directly before registering a plugin.

This repository is **not itself a marketplace**. Publishing it to GitHub does not
automatically make `codex plugin marketplace add owner/repo` a valid install route.
Marketplace packaging or public-directory submission can be added separately.

## Optional unattended summary backend

This sends the selected middle to your configured API endpoint and can incur API
charges. It does **not** extract Codex login tokens or reuse subscription billing.
Choose an API model available to your account with sufficient context capacity;
no model or price assumption is hardcoded.

```powershell
py -3 scripts/compacto.py config init
py -3 scripts/compacto.py config set backend responses
py -3 scripts/compacto.py config set summary_model YOUR_API_MODEL_ID
py -3 scripts/compacto.py config show
```

Provide `OPENAI_API_KEY` through your environment/secret manager, not a committed
file. The environment variable name is configurable with `api_key_env`. Launch
Codex from an environment that contains it when using the hook.

```powershell
py -3 scripts/compacto.py compact --session SESSION_UUID
```

There is one bounded summary request, no automatic retries, no tools, and no URL
following from transcript text. Requests set `tools: []`, `tool_choice: "none"`,
and `store: false`; the latter is **not** a promise of zero provider retention.
Custom `api_base_url` endpoints must use HTTPS (loopback HTTP is allowed for tests).
Redirects are rejected to avoid forwarding credentials to another endpoint.

## Optional PreCompact hook

Installing the plugin alone does not enable automatic compaction or API spending.
Configure the Responses backend first, review/trust the hook through Codex's
`/hooks` interface, then opt in:

```powershell
py -3 scripts/compacto.py config set enabled true
```

The hook runs before manual/automatic native compaction. On success it stops
native compaction and prints the new session's resume command. It **does not**
switch the active CLI. An enabled hook also stops on handled errors so you can
inspect the failure. Repeated identical snapshots reuse a verified job receipt.

To return to normal Codex compaction:

```powershell
py -3 scripts/compacto.py config set enabled false
```

Codex still controls hook trust, skipping, process-launch failures and timeouts;
a plugin cannot promise to block when its executable never runs. Missing Python,
an untrusted hook, or a killed hook process can leave normal Codex behavior active.
Test the integration on a disposable session before relying on unattended use.

## Configuration and private data

Default location: `~/.codex/context-compacto`, or
`CODEX_HOME/context-compacto` when Codex has a custom home. `COMPACTO_STATE_DIR`
overrides this for both the CLI and hook; individual CLI operations also accept
`--state-dir`. A custom CLI-only state path does not configure the hook unless
Codex inherits the same `COMPACTO_STATE_DIR`. `PLUGIN_DATA` is deliberately not used
to avoid the hook and standalone helper reading different config files.

`config show` lists every setting. `config set KEY VALUE` validates the whole
configuration. Head + tail + summary budgets need an additional 256-token envelope
allowance within `max_output_tokens`. Summary input has a separate estimated
`max_input_tokens` guard; it does not discover your model's actual context limit.

Jobs contain raw retained/middle text, summaries, source paths and session IDs.
**Treat the entire state directory as sensitive. Never upload it.** POSIX files
are created mode 0600 and new directories mode 0700; Windows uses inherited ACLs.
Use a private user-owned directory and do not use a shared/symlinked state directory.
No automatic deletion or retention policy is imposed on your checkpoints.

`receipt.json` records progress before/after thread creation and injection. A
verified receipt is idempotent. After a crash/partial receipt, inspect the recorded
thread before making another job. Do not delete a lock unless its helper process
is no longer running. The original remains the recovery source.

## Compatibility boundaries

- Retains user/assistant content and supported tool-call/result data, not byte-for-byte
  JSONL records. Source item IDs/turn metadata are removed; tool call IDs are retained.
- Hidden reasoning is omitted with a warning. Old system/developer instructions
  are not copied as authority; normal startup supplies current instructions.
- New threads reuse the source cwd/model/provider, but deliberately start
  **read-only/on-request**. Existing broad/custom permission profiles are not
  cloned. Review permissions in Codex before continuing work.
- Injected items reach the next model request but are not ordinary visible past
  turns in the CLI. Use the saved preview to inspect retained content.
- Explicit native `replacement_history` is respected. Opaque compaction,
  rollbacks, multi-agent `agent_message` records, legacy shell calls and unknown
  item/record formats fail closed instead of reviving guessed history.
- Images can remain in retained items. If the middle contains media, the automatic
  text backend refuses it; supply a manually reviewed summary instead. Audio
  message content is not supported by this adapter.
- Whole groups must fit their head/tail budget. Large tool results are not silently
  truncated. Raise the relevant budget or explicitly choose not to retain that side.
- UTF-8 bytes / 3 is a heuristic, including JSON overhead. It is not a tokenizer,
  excludes fresh system instructions, and cannot guarantee a model-context fit.
- The source is limited to 64 MiB; no automatic chunking of oversized summaries.
  Active background agents and other changing sessions require an explicit snapshot
  decision; Compacto does not stop them or coordinate their work.

## Development and tests

```powershell
py -3 -m unittest discover -s tests -v
py -3 scripts/validate_package.py
# Exercise the installed Codex binary, using a loopback mock model and isolated state:
$env:COMPACTO_TEST_CODEX = '1'
py -3 -m unittest discover -s tests -v
```

On POSIX: `COMPACTO_TEST_CODEX=1 python3 -m unittest discover -s tests -v`.
No integration test needs a real API key or sends a paid model request. The test
creates, seeds, verifies, closes, resumes and checks the next actual model input,
including both sides of a tool call. The standalone diagnostic
`scripts/probe_app_server.py` also keeps disposable state for manual inspection.

GitHub Actions runs offline tests/package checks across Windows, Linux and macOS
with Python 3.11-3.13, and builds the CLI wheel. The real-Codex test is opt-in, not
claimed to have run on all those platforms. Interactive plugin installation/hook
trust and live paid-provider summarization require your separate smoke test.

## Publishing to GitHub later

Publish **this folder only**. Review the MIT license/author metadata before making
your first public release. Add your real repository URL once you have created it;
none is invented here. Keep API keys, local config and all compaction jobs out of
Git. `.gitignore` covers common artifacts, but review the staged file list yourself.

## Protocol references

The new-session design uses the documented
[App Server injection interface](https://learn.chatgpt.com/docs/app-server#inject-items-into-a-thread).
Its optional hook follows the [Codex hook contract](https://learn.chatgpt.com/docs/hooks),
not Claude's `decision: block` format. Packaging follows the
[official plugin layout](https://developers.openai.com/plugins/build/plugins).
The backend uses the [Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create).
