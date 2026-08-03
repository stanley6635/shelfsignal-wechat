# ShelfSignal v0 canary

This is a blank, privacy-safe template. Keep completed records in the private
runtime workspace. Do not commit account names, article titles, private paths,
QR images, captured content, or browser output.

- Date:
- Commit:
- macOS version:
- Python version:
- ShelfSignal version:

## Automated pre-canary gate

Run these checks from the source checkout before opening a live browser:

```bash
./.venv/bin/ruff check src tests
./.venv/bin/pytest -v
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" \
  skills/shelfsignal-wechat
SHELFSIGNAL_REQUIRE_DIST=1 ./.venv/bin/pytest -q tests/test_public_repository.py
git diff --check
git status --short
```

- [ ] Ruff passed.
- [ ] Full pytest suite passed.
- [ ] Global Skill validation passed.
- [ ] Built wheel and source archive passed the artifact-aware release audit.
- [ ] Repository privacy audit found no private or runtime artifacts.
- [ ] Git diff had no whitespace errors.
- [ ] Git status contained no private runtime or completed canary artifacts.

If the distribution archives do not match the current source, rebuild them
before running the artifact-aware release audit. Do not continue to a live
canary when any pre-canary check fails.

## Run lifecycle

- `list-accounts` may authenticate a dedicated browser profile for an explicit
  run ID. Use that same run ID for the subsequent one-account `collect`.
- Reuse a run ID only to retry that same run after interruption or failure.
  Such a retry must not request a second QR authorization and must skip already
  completed ID/hash pairs.
- A successful one-account collection completes and seals its run. Full-shelf
  expansion therefore uses a new run ID and is a separate collection. Do not
  record the new full-shelf run as a same-run retry or expect it to inherit the
  completed run's `fresh` authorization.

## Checks

- [ ] Fresh QR authorization reached the saved-account shelf.
- [ ] One-account collection completed.
- [ ] Full-shelf collection completed in a new run after the one-account run.
- [ ] Text article preserved source and meaningful images.
- [ ] Image-heavy article produced separate local OCR evidence.
- [ ] Interrupted same-run retry did not request a second QR scan.
- [ ] Interrupted same-run retry did not duplicate completed ID/hash pairs.
- [ ] Historical Markdown seed did not modify its source.
- [ ] Briefing contained every candidate and started unchecked.
- [ ] Checked-only export was self-contained.
- [ ] Global Skill ran from a separate target project.
- [ ] Target project's native workflow accepted or deduplicated one bundle.
- [ ] Logs and Git contained no cookies, profile, or captured article content.

## Visible partial failures

Record counts, named failure classes, and hashes only.

- Omitted accounts:
- Unavailable bodies:
- Missing assets:
- OCR failures:

## Result

- [ ] Pass
- [ ] Fail
