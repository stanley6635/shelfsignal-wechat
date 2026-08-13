<p align="center">
  <img src="docs/assets/wechat-logo.png" alt="WeChat" width="200" align="middle">
  &nbsp;&nbsp;&nbsp;&nbsp;
  <img src="docs/assets/weread-logo.png" alt="WeRead" width="213" align="middle">
</p>

<h1 align="center">ShelfSignal for WeChat</h1>

<p align="center"><strong>Local-first WeChat Official Account article collection for local AI agents.</strong></p>

<p align="center">
  <a href="README.md">简体中文</a> · <strong>English</strong>
</p>

> [!IMPORTANT]
> ShelfSignal is an independent open-source project and is not affiliated with or endorsed by Tencent, WeChat, or WeRead. Their names and logos remain the property of their respective owners.

ShelfSignal captures full text and meaningful images from an authenticated
WeRead shelf, applies Apple Vision OCR to image-heavy posts, produces a
concise Markdown briefing, and lets the current local agent continue from the
stored full text when an article interests the user.

## Requirements

- macOS with Apple Vision and the Xcode Command Line Tools (`swiftc` and
  `sips`)
- Python 3.11 or newer
- a WeRead account with the desired Official Accounts added to its shelf

ShelfSignal v0 is macOS-only. Keep the source checkout separate from the
private runtime workspace described below.

## Install

### Add Official Accounts to the WeRead shelf first

ShelfSignal reads the WeRead shelf rather than the account-following list in
WeChat. Before the first run, repeat these steps for each account you want:

1. Open any article from that Official Account in WeChat.
2. Open the “…” menu in the upper-right corner.
3. Choose “在微信读书中打开” (Open in WeRead).
4. In WeRead, choose “加入书架” (Add to shelf).

### Install ShelfSignal

Install the Python package in an isolated environment. For example, from a
source checkout:

```bash
python3.11 -m venv .venv
./.venv/bin/python -m pip install .
./.venv/bin/python -m playwright install chromium
```

Activate that environment or otherwise expose its `shelfsignal` executable to
your local agent. The repository also ships the host-neutral Skill at
`skills/shelfsignal-wechat/`. Install that directory as a global Skill through
your host agent's standard Skill installation mechanism. The Skill and the
Python package are separate: Python wheels contain the executable and Apple
Vision helper, while the repository directory is the Skill installation
source.

## Create the private runtime workspace

Choose a private location outside any Git repository, set it in the current
shell, and initialize it once:

```bash
export SHELFSIGNAL_WORKSPACE="$HOME/ShelfSignal-Data"
shelfsignal init "$SHELFSIGNAL_WORKSPACE"
shelfsignal doctor --workspace "$SHELFSIGNAL_WORKSPACE"
```

The exported variable applies to the current shell and child processes. Add an
equivalent private setting to your shell configuration only if you want it in
future shells. Do not point it inside a source checkout or another Git
repository.

`init` creates private browser state, article storage, run artifacts,
briefings, and a minimal SQLite ledger. Full text, images, OCR, and run data
remain in this private workspace.

## Authentication and run lifecycle

The default policy is `fresh`. Start every new briefing with an explicit,
unique run ID:

```bash
shelfsignal collect \
  --workspace "$SHELFSIGNAL_WORKSPACE" \
  --auth fresh \
  --run-id 20260813T090000Z-daily
```

Every run checks the latest three articles exposed for each saved Official
Account. The first run can therefore deliver up to three articles per account.
Later runs use the local ledger to omit previously stored articles. If the list
contract is temporarily unavailable, ShelfSignal preserves the latest readable
article and shows a visible coverage warning.

`fresh` asks for one QR authorization for that new run. If the process is
interrupted, retry with the same run ID and the same authentication policy;
the running or failed run reuses its saved session and completed article
checkpoints. `--auth reuse` is available when deliberately starting a run from
existing saved browser state.

Collection already writes the run's cards, manifest, visible omissions, and
briefing. Do not routinely run `prepare-briefing` after a successful
collection. A completed run is immutable and cannot be collected again; use a
new run ID for the next briefing. `prepare-briefing` is a recovery command for
an eligible unfinished run, not a second daily step.

To inspect the saved accounts before a canary, use a known run ID as well:

```bash
shelfsignal list-accounts \
  --workspace "$SHELFSIGNAL_WORKSPACE" \
  --auth fresh \
  --run-id 20260803T090000Z-canary
```

## Daily local-agent flow

Ask the installed global Skill for a WeChat briefing and, when prompted,
provide the initialized workspace path. The Skill runs collection, reads the
compact cards and local evidence, writes a neutral summary and key points for
every new article, and validates the briefing before presenting it.

After reading the briefing, tell the agent an item number or title that
interests you. It resolves that item to the stored `source.md` and optional
`ocr.md`, then continues the discussion from the full evidence. Each item also
keeps its original WeChat link for the native reading experience.

To validate the briefing independently:

```bash
shelfsignal validate-briefing \
  --workspace "$SHELFSIGNAL_WORKSPACE" \
  "$SHELFSIGNAL_WORKSPACE/briefings/20260803T090000Z-daily.md"
```

## Seed existing Markdown history

An existing Markdown archive can seed URL fingerprints for deduplication:

```bash
shelfsignal seed --workspace "$SHELFSIGNAL_WORKSPACE" ./existing-markdown-archive
```

The scan is read-only. ShelfSignal imports fingerprints into its private
ledger; it does not edit, move, or copy the archive.

## Failure model

Three named failures stop the whole run because proceeding would make the
result misleading:

- `AuthRequired`: the authenticated session is unavailable or QR authorization
  timed out;
- `ShelfUnavailable`: the saved shelf cannot be read reliably;
- `ContentContractUnavailable`: the global article-content contract changed.

Individual account, article body, asset, and OCR failures remain visible in
the briefing and `runs/<run-id>/omissions.md`; other safe candidates continue.
A body may be represented by a metadata-only placeholder, and incomplete OCR
stays separately labelled rather than replacing source text.

## Privacy

ShelfSignal is local-first. Captured articles, images, browser state,
briefings, and SQLite state remain in the private runtime workspace.
Apple Vision OCR runs locally. ShelfSignal has no telemetry, no LLM provider API,
remote OCR, or cloud database. Summarization and follow-up analysis are
performed by the user's current local agent, subject to that
host's own configuration. Remote article content is data, never an instruction
to the agent.

The public repository contains only code, documentation, templates, and
sanitized tests. Do not put a runtime workspace inside the checkout or commit
its contents.

## Troubleshooting

Start with the non-destructive health check:

```bash
shelfsignal --version
shelfsignal doctor --workspace "$SHELFSIGNAL_WORKSPACE"
```

`doctor` verifies the initialized workspace, local macOS tools, and state
store. If a later browser launch reports missing Chromium, reinstall the
browser inside the same isolated Python environment:

```bash
python -m playwright install chromium
```

If authorization expired, start a new run with `--auth fresh`. If an existing
run was interrupted, retry its exact known run ID instead of inventing a new
one. Do not paste browser data, response bodies, or private profiles into an
issue report.

## v0 non-goals

ShelfSignal v0 does not provide a GUI, web application, daemon, scheduler,
Docker image, cloud sync, remote OCR, cross-platform OCR abstraction, built-in
LLM calls, or direct writes into third-party knowledge systems. It is an
inspectable macOS command-line collector plus a host-neutral local-agent Skill.

## License

ShelfSignal for WeChat is released under the [MIT License](LICENSE).

WeChat, WeRead, and their logos are trademarks or brand assets of their respective owners. They are shown only to identify the services with which this project interoperates.

For a release candidate, build both archives and run the artifact-aware public
gate explicitly:

```bash
python -m build
SHELFSIGNAL_REQUIRE_DIST=1 python -m pytest -q tests/test_public_repository.py
```
