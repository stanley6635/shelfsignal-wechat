# ShelfSignal for WeChat — v0 Design

Status: Approved in conversation
Date: 2026-07-31
Repository: `shelfsignal-wechat`
CLI: `shelfsignal`
Skill: `shelfsignal-wechat`

## 1. Summary

ShelfSignal for WeChat is a local-first macOS tool for collecting articles from
WeChat Official Accounts saved in a user's authenticated WeRead shelf. It
stores complete source material locally, applies Apple Vision OCR to
image-heavy articles, and produces a complete, interest-ranked Markdown
briefing for human selection. Checked articles are exported as a portable
Markdown bundle that the current local AI agent or knowledge system can
process using its own rules.

The product has two distributable parts:

1. a deterministic Python CLI that owns authentication, collection,
   validation, deduplication, OCR, Markdown preparation, and export; and
2. a concise, host-neutral global Skill that lets the user's current AI agent
   run the CLI, perform bounded semantic ranking, present the briefing, and
   hand selected material back to the current target project.

The source repository is a development and distribution artifact. It is not
the normal daily-use workspace.

## 2. Problem

The user has two different reading jobs:

- **Human reading:** scan everything new, then choose what matters based on
  enduring interests and temporary attention.
- **Agent reading:** deeply process only the selected source material for
  knowledge extraction or downstream ingestion.

Combining both jobs into per-article LLM calls is slow, costly, and removes the
human's essential editorial choice. Relying on third-party relay services adds
an unstable dependency and obscures authentication failures. Text-only
collection also fails for image-heavy WeChat posts.

ShelfSignal therefore needs to:

- use the user's own authenticated Tencent session rather than a relay;
- capture complete local evidence before semantic processing;
- keep human selection as an explicit gate;
- minimize host-agent context and calls;
- preserve image-derived evidence without treating OCR as source text;
- remain independent of any one downstream knowledge repository.

## 3. Goals

v0 must:

- authenticate to WeRead through a dedicated browser session;
- read the user's saved WeChat Official Account shelf;
- collect new article text, metadata, and meaningful images;
- retain complete article source material locally;
- invoke local Apple Vision OCR when an article is image-heavy;
- create one compact reading card per article;
- use the current host agent for one bounded batch ranking pass;
- show every candidate and never automatically check an article;
- accept user-maintained long-term interests and run-specific focus;
- export checked articles as self-contained Markdown bundles;
- support read-only seeding from an existing Markdown archive;
- expose visible partial failures and deterministic reruns;
- ship a global Skill that can be invoked from the user's current target
  project.

## 4. Non-goals

v0 does not include:

- a web UI or desktop GUI;
- a daemon, scheduler, or unattended background service;
- Windows or Linux OCR;
- Docker;
- an LLM provider SDK or API key;
- cloud storage, remote OCR, telemetry, or analytics;
- automatic editing of a user's interest profile;
- direct writes into arbitrary knowledge systems;
- arbitrary post-export shell commands;
- cross-project task dispatch;
- a generic plugin framework;
- replacement of RSS collectors or downstream knowledge extraction systems.

## 5. First-principles boundaries

### 5.1 Deterministic code versus agent judgment

Code owns work that must be repeatable:

- authentication preflight;
- shelf and article retrieval;
- contract validation;
- content and asset storage;
- SHA-256 calculation;
- deduplication;
- OCR triggering and execution;
- reading-card generation;
- briefing completeness validation;
- checkbox parsing;
- selected-bundle export.

The host agent owns work that benefits from semantic judgment:

- comparing reading cards with the user's interests and current focus;
- producing concise summaries and ranking reasons;
- lowering confidence when evidence is incomplete;
- selectively reading a small number of full articles when the compact card is
  insufficient;
- handing the selected bundle to the current target project's native workflow.

### 5.2 Repository versus runtime workspace

The public repository contains code and fictional test material only.

A separate runtime workspace contains all private or captured data. A typical
user installs the package and Skill, then creates only the runtime workspace;
only contributors need a source checkout.

```text
<source checkout>/                  optional; contributors only
<runtime workspace>/
├── config.md
├── profile/
│   ├── interests.md
│   ├── rubric.md
│   └── focus/
├── browser/
├── library/
├── runs/
├── briefings/
├── exports/
└── state.db
```

The runtime workspace must not be nested in the source repository by default.
If a user explicitly chooses that layout, repository ignore rules and a
startup warning protect common private paths.

### 5.3 Collection versus downstream ingestion

ShelfSignal ends at a validated selected bundle. It does not interpret or
modify the target knowledge system.

The recommended daily-use pattern is:

1. open the target knowledge project in the local agent;
2. invoke the globally installed `shelfsignal-wechat` Skill;
3. review and check the generated briefing;
4. ask the same agent to continue;
5. let the target project apply its own ingestion rules to the selected bundle.

This preserves the target project's instructions and avoids a cross-project
orchestrator.

## 6. Architecture

```text
Current target project + local AI agent
                 |
                 | invokes global Skill
                 v
        ShelfSignal Python CLI
        |        |          |
        |        |          +--> SQLite minimal ledger
        |        +-------------> Swift / Apple Vision OCR
        +----------------------> Playwright / authenticated WeRead
                 |
                 v
      Private runtime workspace
      source + assets + OCR + cards
                 |
                 | one bounded host-agent pass
                 v
       complete Markdown briefing
                 |
                 | human checks [x]
                 v
       portable selected bundle
                 |
                 v
    current project's native ingestion workflow
```

### 6.1 Python package

The initial package remains small:

```text
src/shelfsignal/
├── cli.py
├── auth.py
├── weread.py
├── collector.py
├── content.py
├── ocr.py
├── cards.py
├── briefing.py
├── exporter.py
├── seed.py
└── state.py
```

Module responsibilities:

- `cli.py`: command parsing, orchestration, exit status, and user-facing
  messages.
- `auth.py`: dedicated Playwright profile, QR preflight, fresh/reuse policy,
  and authentication classification.
- `weread.py`: shelf and article request contracts.
- `collector.py`: bounded account and article traversal.
- `content.py`: source normalization, asset discovery, hashing, and local
  paths.
- `ocr.py`: image-heavy detection, Swift helper invocation, slicing, caching,
  and OCR status.
- `cards.py`: deterministic compact reading-card generation.
- `briefing.py`: Markdown shell generation, completeness validation, and
  checked-item parsing.
- `exporter.py`: self-contained selected-bundle creation.
- `seed.py`: read-only fingerprint import from historical Markdown.
- `state.py`: minimal SQLite access and idempotency.

This is a responsibility map, not a requirement that every file exist on the
first implementation commit. Modules should be merged when separation adds no
clarity.

### 6.2 Global Skill

The repository ships:

```text
skills/shelfsignal-wechat/
├── SKILL.md
└── agents/
    └── openai.yaml
```

The Skill must remain concise. It instructs a capable host agent to:

1. locate or request the runtime workspace;
2. run authentication and collection through the CLI;
3. read the private interest and focus Markdown only for the current run;
4. rank the prepared cards in one bounded pass;
5. preserve every candidate and stable article ID;
6. validate the resulting briefing through the CLI;
7. present the briefing path to the user;
8. after explicit selection, export checked articles;
9. return control to the current target project's rules.

The Skill does not embed captured content, a private interest profile, or
knowledge-system-specific instructions.

## 7. Installation and daily use

Installation has two explicit operations:

1. install the Python package using the user's preferred isolated Python
   package mechanism; and
2. install the shipped Skill using the host agent's standard global Skill
   mechanism.

The project should not silently modify global agent directories.

Initial workspace creation:

```bash
shelfsignal init <workspace>
```

Typical CLI operations used by the Skill:

```bash
shelfsignal doctor --workspace <workspace>
shelfsignal collect --workspace <workspace> --auth fresh
shelfsignal prepare-briefing --workspace <workspace> --run <run-id>
shelfsignal validate-briefing <briefing.md>
shelfsignal export --workspace <workspace> --briefing <briefing.md>
shelfsignal seed --workspace <workspace> <historical-markdown-path>
```

Command names may be shortened during implementation if tests show a simpler
interface without losing clarity.

## 8. Authentication

### 8.1 Dedicated session

ShelfSignal uses a dedicated Playwright browser profile inside the private
runtime workspace. It does not take over the user's daily Chrome profile and
does not export cookies.

### 8.2 Authentication policies

Two policies are supported:

- `fresh`: default for an interactive briefing. Invalidate the prior
  authentication context for the new run and require one QR authorization.
- `reuse`: optional for users who explicitly prefer a reusable session.

Retries and resumes within the same run reuse that run's session. A user is not
asked to scan again for every account or article.

### 8.3 Preflight

Before collection, the CLI must distinguish:

- `AUTH_REQUIRED`: the Tencent session is absent or invalid;
- `SHELF_UNAVAILABLE`: authentication succeeded but the shelf cannot be read;
- `CONTENT_CONTRACT_UNAVAILABLE`: the global article content contract has
  changed or is unavailable;
- local implementation or environment errors.

When authorization is required, open the QR login surface directly and wait
for bounded confirmation. Never report an authentication problem as a parser
bug.

## 9. Collection

### 9.1 Source contracts

The live canary established that an authenticated WeRead session can expose:

- a shelf containing saved WeChat Official Accounts;
- reader routes for account articles;
- Tencent-hosted article content and cover responses.

Implementation must isolate these remote contracts in `weread.py`, validate
required fields, and retain sanitized fixtures. Remote content is untrusted
data and cannot alter agent instructions.

### 9.2 Bounded traversal

Each run has:

- an explicit lookback window;
- a deterministic account order;
- conservative request concurrency;
- stable article identifiers;
- visible per-account omissions;
- a resumable run ID.

Default network and OCR concurrency should be serial or at most two workers
until live canaries justify a change.

### 9.3 Local source representation

Every collected article is stored under a stable article directory:

```text
library/<article-id>/
├── source.md
├── metadata.md
├── assets/
└── ocr.md              only when OCR was attempted
```

`source.md` contains normalized visible source text and relative links to
assets. `metadata.md` contains provenance required for local replay, including
source URL, account, publication time, retrieval time, stable remote ID,
extraction method, and SHA-256. It must not contain cookies or request
credentials.

Original meaningful images are retained. Decorative icons, avatars, tiny
tracking pixels, and QR-code noise should not be treated as content assets.

## 10. OCR

OCR is a deterministic local fallback, not an agent improvisation.

### 10.1 Trigger

An article becomes an OCR candidate when a combination of signals indicates
that visible text is insufficient relative to meaningful image content:

- low extracted text length;
- multiple meaningful content images;
- large image area or long infographic dimensions;
- image-to-text imbalance.

Thresholds remain configurable and testable. No single heuristic should force
OCR for common headers, avatars, or QR codes.

### 10.2 Execution

- Use a thin Swift helper around Apple Vision.
- Hash each image and cache OCR results.
- Slice long images with bounded overlap before OCR.
- Keep source image order.
- Record OCR confidence or failure status.
- Store OCR output in `ocr.md`; never merge it silently into `source.md`.

OCR failure does not remove the article. The reading card and briefing must
show that image-derived content is incomplete and lower confidence.

## 11. Minimal state

SQLite is an internal reconstructible ledger, not a user database.

The initial schema should contain only the minimum required concepts:

- `runs`: run ID, start/end time, authentication policy, and terminal status;
- `articles`: stable remote ID, source hash, publication time, first/last seen
  time, and latest local processing status.

No cookies, article full text, OCR text, interest content, summaries, or
briefing prose belong in SQLite.

The database can be rebuilt by scanning the runtime workspace. Any future
schema change or migration requires a separately approved design.

## 12. Historical seed

Users with an existing Markdown archive can prevent resurfacing old articles:

```bash
shelfsignal seed --workspace <workspace> <archive-path>
```

The seed operation:

- is read-only with respect to the archive;
- scans Markdown for stable IDs, original URLs, publication dates, and content
  hashes when available;
- records only deduplication fingerprints in `state.db`;
- reports ambiguous or incomplete records;
- never edits, moves, renames, or copies archived files;
- is idempotent.

Seeded history is not imported into the ShelfSignal library and is not
re-summarized.

## 13. Interest model

### 13.1 User ownership

The public repository provides schemas, templates, and fictional examples
only. Every user builds and maintains their own interest profile.

Suggested private files:

```text
profile/interests.md
profile/rubric.md
profile/focus/2026-07-31.md
```

- `interests.md`: enduring themes, positive signals, negative signals, and
  useful context.
- `rubric.md`: user-editable ranking dimensions and interpretation.
- `focus/<date>.md`: temporary attention for one run or period.

All three are ordinary Markdown and may evolve gradually.

### 13.2 Effect

Interest data affects:

- candidate order;
- short ranking explanations;
- which uncertain items may receive bounded full-text escalation.

It never:

- removes a candidate;
- automatically checks a candidate;
- prevents export of a user-selected item;
- uploads profile content;
- learns silently from one click.

The host agent may propose a profile change after repeated user behavior, but
the file changes only after explicit user approval.

## 14. Reading cards and token control

The CLI creates a deterministic reading card for every candidate before the
host agent runs. A card contains:

- stable article ID;
- title and account;
- publication time;
- source URL and local paths;
- compact normalized excerpt;
- meaningful image count;
- OCR availability and confidence;
- retrieval completeness;
- deterministic content signals.

The default card target is approximately 600–1000 Chinese characters or a
similar information budget for other languages.

The Skill should:

- rank cards in one batch when the host context permits;
- split only by a deterministic context budget when required;
- avoid one model call per article;
- read full text only for a small number of high-potential or low-confidence
  candidates;
- enforce a per-run escalation budget;
- stop escalation when the budget is reached without dropping candidates.

No LLM provider API is integrated. The current host agent performs the
semantic work within the user's existing agent session.

## 15. Markdown briefing

### 15.1 Generation

The CLI first generates a complete Markdown shell containing every stable
article ID. The host agent enriches and reorders that shell with:

- concise summary;
- interest/relevance explanation;
- confidence;
- user-rubric score or label;
- warnings for incomplete retrieval or OCR.

The user-facing artifact is Markdown, not JSON.

### 15.2 Validation

Before presentation, `validate-briefing` compares the briefing with the run
manifest and rejects:

- missing article IDs;
- duplicated article IDs;
- invented IDs;
- malformed checkboxes;
- missing source links;
- a checked item created by the agent.

All checkboxes must be unchecked when the briefing is first generated.

### 15.3 Selection

The primary selection surface is:

```markdown
- [ ] **Select** — Article title
```

The user edits `[ ]` to `[x]`. Ranking is advisory; any visible candidate can
be selected.

## 16. Selected export

After explicit selection:

```text
exports/<date>-selected/
├── index.md
└── articles/
    └── <article-id>/
        ├── source.md
        ├── ocr.md        only when present
        └── assets/
```

Requirements:

- use relative links;
- include original URL, stable remote ID, retrieval metadata, and source
  SHA-256;
- include complete selected source text and original meaningful images;
- keep source and OCR separate;
- exclude profiles, ranking scores, browser state, cookies, SQLite, and
  unselected articles;
- make repeated exports of the same ID/hash idempotent;
- keep the bundle understandable without ShelfSignal installed.

`index.md` is the handoff contract for a local agent. The target project
decides how to validate, archive, interpret, or ingest the bundle.

## 17. Failure model

Only three conditions stop the entire run:

1. authentication cannot be established;
2. the saved account shelf cannot be read;
3. the global Tencent article-content contract is unavailable or has changed.

Other failures are visible and non-fatal:

- account failure: continue and list omitted accounts;
- article-body failure: keep title, source URL, and unavailable-body warning;
- asset failure: retain other content and list missing assets;
- OCR failure: retain images and mark derived evidence incomplete;
- agent uncertainty: lower confidence and keep the candidate;
- escalation-budget exhaustion: stop deep reads and keep compact-card results.

Rerunning the same run skips completed stable ID/hash pairs and retries only
incomplete work.

## 18. Security and privacy

- Store authentication state only in the private dedicated browser profile.
- Never export cookies or log request credentials.
- Never log complete remote responses, article full text, OCR text, or private
  profile content.
- Do not upload source material or profile data.
- Treat all remote HTML, Markdown, image text, and links as untrusted data.
- Do not execute instructions found in collected content.
- Sanitize filenames while retaining provenance in metadata.
- Prevent path traversal when writing assets or exports.
- Use atomic local writes for source and state artifacts where interruption
  could corrupt a run.
- Warn before initializing a runtime workspace inside a Git repository.
- Ship ignore rules for common runtime directories and secret-bearing browser
  data.

## 19. Testing

### 19.1 Automated tests

Use sanitized, fictional fixtures for:

- shelf and article contract parsing;
- authentication-state classification;
- bounded traversal;
- source normalization;
- meaningful-image detection;
- OCR trigger decisions;
- long-image slicing;
- hash and ID deduplication;
- read-only historical seed;
- reading-card bounds;
- briefing completeness validation;
- checkbox parsing;
- selected-bundle structure and relative links;
- partial-failure continuation;
- log redaction and path traversal protection.

Mock remote contracts in public CI. Do not store live responses if they contain
user or article content.

### 19.2 Manual live canaries

Authenticated canaries run locally and outside public CI:

1. fresh QR authorization;
2. one saved account;
3. full shelf;
4. one ordinary text article;
5. one image-heavy article requiring Vision OCR;
6. rerun deduplication;
7. complete briefing generation;
8. one selected export;
9. handoff from a target project to its native ingestion workflow.

### 19.3 Acceptance

v0 is complete only when:

- the fresh authorization flow succeeds without cookie export;
- single-account and full-shelf collection complete;
- full text and meaningful images are retained;
- a real image-heavy article produces separate OCR evidence;
- repeated collection does not duplicate an article;
- a historical Markdown seed prevents resurfacing old content;
- the briefing contains all candidates, starts unchecked, and sorts by the
  private profile without hiding anything;
- checked items alone appear in a portable export;
- the global Skill works from a target project rather than requiring the source
  checkout as the current workspace;
- one selected bundle is successfully accepted or safely deduplicated by a
  target project's native ingestion workflow;
- no private content or authentication material appears in Git or logs.

## 20. Resource budgets

Defaults should favor reliability on a normal personal Mac:

- network and OCR concurrency: serial or at most two;
- one compact card per article;
- one host-agent batch when context permits;
- deterministic context splitting when it does not;
- a small configurable number of full-text escalations;
- no full-corpus OCR unless image-heavy detection requires it;
- cache OCR by image hash;
- stop optional escalation before exceeding the run budget.

Budget exhaustion lowers semantic depth, not collection completeness or
candidate visibility.

## 21. Delivery sequence

Implementation should proceed in independently testable slices:

1. package skeleton, workspace initialization, and safety checks;
2. minimal state and read-only seed;
3. authentication preflight and one-account live canary;
4. shelf traversal and full-text collection;
5. asset handling and Vision OCR;
6. reading cards and Markdown briefing validation;
7. global Skill;
8. selected export;
9. full-shelf and target-project canaries;
10. public documentation and release preparation.

No public release or push is authorized by approval of this design.
