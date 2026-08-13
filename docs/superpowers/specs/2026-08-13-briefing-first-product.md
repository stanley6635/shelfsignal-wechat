# Briefing-first product specification

Date: 2026-08-13
Status: Approved

## Product contract

ShelfSignal reads the latest three articles exposed for each Official Account
on the authenticated WeRead shelf. It stores complete source evidence locally,
deduplicates previously captured articles, and produces a concise Markdown
briefing containing every unseen article in that three-item window.

Each briefing item contains the account, title, publication time, a neutral
summary, key points, and the original WeChat article link. The briefing ends by
inviting the user to name an item for deeper discussion. The host agent then
uses the stored `source.md` and optional `ocr.md` rather than requiring the user
to reopen the original article.

The public onboarding explains that users must first add each desired Official
Account to their WeRead shelf. From an article in WeChat, open the overflow
menu, choose “在微信读书中打开”, and then add the account to the shelf in
WeRead. Text instructions are sufficient; public screenshots are not required.

## Runtime behavior

- The collection window is fixed at three articles per account.
- The first run can therefore produce up to three articles per account.
- Later runs show only unseen articles from the current three-item window.
- If the list contract is unavailable but the shelf cover remains available,
  collect the latest article and surface a coverage warning.
- Full text, source images, and derived OCR remain in the private workspace.
- Briefings preserve hidden article IDs and immutable digests so the agent can
  resolve a title or item number to its local evidence safely.

## Briefing presentation

The host agent may edit only the summary and key-points sections. It must not
alter article identity, metadata, evidence, or original links. The final prompt
is:

> 对哪一篇文章感兴趣？告诉我序号或标题，我们可以基于已保存的全文继续聊。

The original article link remains available for users who prefer the native
WeChat reading experience.
