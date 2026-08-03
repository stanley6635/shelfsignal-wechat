# ShelfSignal for WeChat

[English](README.md) | [简体中文](README.zh-CN.md)

面向本地 AI Agent 的微信公众号文章采集与简报工具。

ShelfSignal 通过用户授权后的微信读书书架获取公众号文章，完整保存正文和有意义的图片；图片型内容可调用 Apple Vision 在本地完成 OCR。采集结果会整理成一份包含全部候选、按个人兴趣排序的 Markdown 简报，用户选中需要的文章后，再导出为可交给当前 Agent 或知识库继续处理的独立材料包。

## 运行要求

- macOS，并已安装支持 Apple Vision 的系统环境和 Xcode Command Line Tools（需要 `swiftc` 与 `sips`）
- Python 3.11 或更高版本
- 微信读书账号，且书架中已保存微信公众号内容

v0 仅支持 macOS。源码仓库和下文介绍的私有运行目录必须分开存放。

## 安装

建议把 Python 包安装在隔离环境中。以源码安装为例：

```bash
python3.11 -m venv .venv
./.venv/bin/python -m pip install .
./.venv/bin/python -m playwright install chromium
```

使用时需要激活该环境，或通过其他方式让本地 Agent 能够调用 `shelfsignal`。仓库同时提供与宿主无关的 Skill：`skills/shelfsignal-wechat/`，可通过宿主 Agent 的标准安装方式注册为全局 Skill。

Skill 与 Python 包是两个独立部分：wheel 提供命令行程序和 Apple Vision 辅助工具，仓库中的 Skill 目录负责告诉本地 Agent 如何编排完整流程。

## 建立私有运行目录

选择一个不属于任何 Git 仓库的私有位置，在当前 shell 中设置路径，并初始化一次：

```bash
export SHELFSIGNAL_WORKSPACE="$HOME/ShelfSignal-Data"
shelfsignal init "$SHELFSIGNAL_WORKSPACE"
shelfsignal doctor --workspace "$SHELFSIGNAL_WORKSPACE"
```

环境变量只对当前 shell 及其子进程生效。如果希望以后自动加载，可以自行写入 shell 配置；不要把运行目录放进 ShelfSignal 源码仓库或其他 Git 项目。

`init` 会创建浏览器状态、文章库、运行记录、简报、导出目录和一份最小化的 SQLite 台账，同时生成可编辑的 Markdown 用户画像：

- `profile/interests.md`：长期关注和明确不关注的方向；
- `profile/rubric.md`：评分与判断标准；
- `profile/focus/`：可选，用于某一次运行的临时关注重点。

这些资料只影响排序、摘要角度和置信度，不会隐藏候选，也不会自动勾选文章。除非用户明确同意，Skill 只读取画像，不会修改它们。

## 授权与运行周期

默认认证策略是 `fresh`。每次生成新简报时，应使用一个明确且唯一的 run ID：

```bash
shelfsignal collect \
  --workspace "$SHELFSIGNAL_WORKSPACE" \
  --auth fresh \
  --run-id 20260803T090000Z-daily \
  --lookback-days 7
```

v0 的微信读书适配器采用 **latest-only** 策略：每个已保存公众号最多读取微信读书当前暴露的一篇文章，不承诺获取历史文章列表。`--lookback-days` 只判断当前文章是否仍在关注时限内，不会向前遍历历史。后续运行通过本地台账跳过已经采集过的来源，因此可以随着公众号更新逐步累积内容。

`fresh` 会在每个新 run 开始时要求扫码一次。同一个 run 如果中断，使用原 run ID 和相同认证策略重试即可；已完成的文章 checkpoint 和该 run 的授权状态会被复用。只有明确希望沿用可复用浏览器会话时，才使用 `--auth reuse`。

采集完成后，系统已经生成 cards、manifest、可见遗漏记录和一份全部未勾选的简报。不要在正常成功后再次运行 `prepare-briefing`。已完成 run 不允许重复采集；下一次简报应使用新的 run ID。`prepare-briefing` 只用于恢复符合条件的未完成运行。

选择文章时，直接告诉本地 Agent 要选哪些序号或 article ID。Agent 只把对应行从 `- [ ]` 改为 `- [x]`，随后运行校验。不要用可能重写 Markdown 结构的富文本编辑器保存这份绑定简报。

如需先查看书架中的公众号，可使用一个已知 run ID：

```bash
shelfsignal list-accounts \
  --workspace "$SHELFSIGNAL_WORKSPACE" \
  --auth fresh \
  --run-id 20260803T090000Z-canary
```

## 日常使用流程

向已安装的 ShelfSignal Skill 提出“生成微信公众号简报”之类的请求，并在需要时提供初始化后的 workspace 路径和本次 focus 文件。Skill 会完成采集，读取紧凑 cards 和私有兴趣画像，对全部候选排序，并保持所有 checkbox 未勾选；呈现前还会校验简报结构和绑定信息。

阅读简报后，把想保留的条目告诉本地 Agent。Agent 会精确修改相应 checkbox，再次校验，然后只导出已选文章：

```bash
shelfsignal validate-briefing \
  --workspace "$SHELFSIGNAL_WORKSPACE" \
  "$SHELFSIGNAL_WORKSPACE/briefings/20260803T090000Z-daily.md"
shelfsignal export \
  --workspace "$SHELFSIGNAL_WORKSPACE" \
  --briefing "$SHELFSIGNAL_WORKSPACE/briefings/20260803T090000Z-daily.md"
```

每个已完成 run 的导出目录只创建一次。导出的 selected bundle 可以独立使用，包含索引，以及每篇入选文章的 `source.md`、`metadata.md`、可选的独立 `ocr.md` 和所引用的图片。它不会包含用户画像、浏览器状态、评分、Cookie 或 SQLite 数据库。后续入库或知识加工交给当前目标项目原有的本地 Agent 流程。

## 导入既有 Markdown 历史

已有 Markdown 归档可以作为去重指纹来源：

```bash
shelfsignal seed --workspace "$SHELFSIGNAL_WORKSPACE" ./existing-markdown-archive
```

扫描过程是只读的。ShelfSignal 只把来源指纹写入私有台账，不会编辑、移动或复制原归档。

## 失败处理

以下三类问题会停止整个 run，因为继续执行可能让结果看起来完整、实际却不可靠：

- `AuthRequired`：没有有效授权，或扫码等待超时；
- `ShelfUnavailable`：无法可靠读取已保存的公众号书架；
- `ContentContractUnavailable`：远端全局文章内容接口发生变化。

单个账号、正文、图片或 OCR 的失败不会被隐藏，而是记录在简报和 `runs/<run-id>/omissions.md` 中；其余安全候选继续处理。正文不可用时可保留仅含元数据的占位材料，OCR 不完整时也会单独标注，不会覆盖原始正文。

## 隐私边界

ShelfSignal 采用 local-first 设计。采集的文章、图片、浏览器状态、用户画像、简报、导出材料和 SQLite 台账都保留在私有运行目录。Apple Vision OCR 完全在本机执行。项目不包含遥测、LLM 服务商 API、远程 OCR 或云数据库。

语义排序和摘要由用户当前使用的本地 Agent 完成，其数据边界取决于该宿主自身的配置。远端文章内容始终被视为不可信数据，不会被当作 Agent 指令执行。

公开仓库只包含代码、文档、模板和经过清理的测试材料。不要把运行目录放进源码 checkout，也不要提交其中的任何内容。

## 故障排查

先运行无破坏性的健康检查：

```bash
shelfsignal --version
shelfsignal doctor --workspace "$SHELFSIGNAL_WORKSPACE"
```

`doctor` 会检查 workspace、macOS 本地工具和状态台账。如果后续启动浏览器时提示缺少 Chromium，请在安装 ShelfSignal 的同一 Python 隔离环境中执行：

```bash
python -m playwright install chromium
```

授权失效时，新建 run 并使用 `--auth fresh`。如果只是已有 run 被中断，应重试原来的 run ID，不要另造一个 ID。提交 issue 时不要粘贴浏览器数据、完整接口响应或私人画像。

## v0 明确不做什么

ShelfSignal v0 不提供 GUI、Web 应用、后台守护进程、定时器、Docker 镜像、云同步、远程 OCR、跨平台 OCR 抽象、内置 LLM 调用，也不会直接写入第三方知识库。

它是一套可检查的 macOS 命令行采集器，加上一份与宿主无关的本地 Agent Skill。

## 许可证

ShelfSignal for WeChat 采用 [MIT License](LICENSE) 发布。

制作 release candidate 时，需要构建 wheel 与 sdist，并显式运行针对发布产物的公开检查：

```bash
python -m build
SHELFSIGNAL_REQUIRE_DIST=1 python -m pytest -q tests/test_public_repository.py
```
