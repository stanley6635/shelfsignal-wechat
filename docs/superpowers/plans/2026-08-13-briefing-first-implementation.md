# Briefing-first implementation plan

Date: 2026-08-13
Status: Approved

1. Cap the WeRead article-list adapter at the newest three valid articles per
   account, remove date-window behavior from the public collection workflow,
   and retain a visible latest-only fallback warning.
2. Replace checkbox and ranking fields with editable summary and key-points
   sections while preserving hidden IDs, immutable metadata digests, and
   structural validation.
3. Remove the selection-based CLI export path from the normal product and
   update the global Skill to generate the briefing, validate it, and resolve
   later user follow-ups to the stored source paths in `cards.md`.
4. Rewrite the Chinese-first README and optional English README around WeRead
   shelf setup, fixed three-item collection, local deduplication, original
   links, and continued agent discussion.
5. Update focused tests for collection limits, fallback warnings, briefing
   integrity, CLI behavior, Skill instructions, and public documentation; then
   run the repository quality gate.
