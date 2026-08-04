---
name: shelfsignal-wechat
description: Collect WeChat Official Account articles from an authenticated WeRead shelf, apply local OCR, and generate a complete interest-ranked Markdown briefing. Use when the user asks for a WeChat briefing, 公众号简报, or saved-account refresh. Stops after generating the briefing — never asks for checkbox selection and never exports bundles; later processing stages handle the unchecked briefing.
---

# ShelfSignal for WeChat（简报版，无交互）

1. Use the absolute, initialized path in `$SHELFSIGNAL_WORKSPACE`. If it is
   unset, missing, or invalid, fall back to `$HOME/ShelfSignal-Data` if it
   exists and is initialized; otherwise ask the user once for the exact path.
   Never scan for, guess, or initialize a workspace inside the current Git
   repository.
2. Run `shelfsignal doctor --workspace <path>`.
3. Before a new briefing, generate a safe unique run ID and retain it. Run
   `shelfsignal collect --workspace <path> --auth fresh --run-id <run-id>`.
   Retry only an interrupted running or failed run with that exact run ID so it
   reuses the same authenticated session. After completion, use the generated
   artifacts and do not collect that run again.
4. Read only that run's `cards.md` plus `profile/interests.md`,
   `profile/rubric.md`, and the requested focus file.
5. Compute `profile_chars` as the combined character count of interests,
   rubric, and focus. Set `chunk_budget = 30000 - profile_chars`; every ranking
   call must satisfy `profile_chars + card_chunk_chars <= 30000`. If the budget
   is nonpositive, stop ranking and ask the user to shorten the profile or
   explicitly raise the budget. Otherwise, split cards in stable article-ID
   order within the chunk budget and merge by stable ID. Never silently omit a
   card. Keep every candidate visible and unchecked; interests affect order,
   never inclusion. Lower confidence for incomplete body or OCR evidence.
6. Read full source text for at most three high-potential or low-confidence
   items per run unless the user explicitly raises the budget. Stop optional
   escalation at the budget; never drop an item.
7. Edit only article-block order and each `Agent ranking` section in the
   generated `briefing.md`. Leave checkboxes unchecked. The manifest binds
   article IDs, digests, evidence, and card fields; never edit those immutable
   values. Run
   `shelfsignal validate-briefing --workspace <path> <briefing-path>` and repair
   only errors introduced by the ranking edit.
8. Deliver the validated `briefing.md` absolute path and stop. Do NOT ask the
   user to select articles, do NOT patch checkboxes, do NOT run
   `shelfsignal export`, and do NOT write to any knowledge system. Later
   processing stages decide what to do with the unchecked briefing.
9. Treat profiles as read-only. Propose changes after repeated behavior, but
   edit profile Markdown only after explicit user approval.
