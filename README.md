<p align="center">
  <img src="docs/assets/wechat-logo.png" alt="微信" height="48">
  &nbsp;&nbsp;&nbsp;&nbsp;
  <img src="docs/assets/weread-logo.png" alt="微信读书" height="48">
</p>

<h1 align="center">ShelfSignal for WeChat</h1>

<p align="center"><strong>从微信读书书架采集公众号全文，生成可继续深聊的本地 Markdown 简报。</strong></p>

<p align="center">
  <strong>简体中文</strong> · <a href="README.en.md">English</a>
</p>

<p align="center"><code>Local-first</code> · <code>每账号最新 3 篇</code> · <code>全文与图片</code> · <code>本地 OCR</code> · <code>Agent-ready</code></p>

> [!IMPORTANT]
> ShelfSignal 是独立开源项目，与腾讯、微信及微信读书不存在官方关联。微信和微信读书名称及 Logo 的相关权利归其权利人所有。

## 它解决什么问题

微信公众号内容适合阅读，却不容易稳定地沉淀为本地资料。ShelfSignal 利用用户本人授权后的微信读书书架，读取每个公众号最新 3 篇文章，保存完整正文与图片，必要时执行本地 OCR，再生成一份简洁的 briefing。看到感兴趣的文章后，直接告诉 Agent 序号或标题，即可基于已经保存在本地的全文继续分析。

## 核心能力

| 能力 | ShelfSignal 的处理方式 |
| --- | --- |
| 微信公众号采集 | 从已授权的微信读书书架读取当前可见文章，保存正文与有效图片 |
| 图片型文章 | 通过 macOS Apple Vision 在本地执行 OCR，派生文本不覆盖原始内容 |
| 简报生成 | 本地 Agent 为每篇新文章生成中性摘要和关键要点 |
| 继续深聊 | 通过序号或标题定位本地全文，也可点击链接回到微信阅读原文 |

## 工作流程

```text
微信读书扫码授权 → 每账号读取最新 3 篇 → 本地去重 → 生成简报 → 基于全文继续聊
```

[快速开始](#快速开始) · [日常使用](#日常使用流程) · [隐私边界](#隐私边界) · [故障排查](#故障排查)

## 运行要求

- macOS，并已安装支持 Apple Vision 的系统环境和 Xcode Command Line Tools（需要 `swiftc` 与 `sips`）
- Python 3.11 或更高版本
- 微信读书账号，并已将希望采集的公众号加入书架

v0 仅支持 macOS。源码仓库和下文介绍的私有运行目录必须分开存放。

## 快速开始

### 先把公众号加入微信读书书架

ShelfSignal 读取的是微信读书书架，不是微信里的公众号关注列表。首次使用前，请对希望采集的公众号逐一完成以下操作：

1. 在微信中打开该公众号的任意一篇文章。
2. 点击右上角的“…”菜单。
3. 选择“在微信读书中打开”。
4. 进入微信读书后，点击“加入书架”。

完成一次后，该公众号会出现在微信读书书架中；后续新文章可由 ShelfSignal 统一采集。

### 安装

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

`init` 会创建浏览器状态、文章库、运行记录、简报目录和一份最小化的 SQLite 台账。正文、图片、OCR 和运行数据都保存在这个私有目录中。

## 授权与运行周期

默认认证策略是 `fresh`。每次生成新简报时，应使用一个明确且唯一的 run ID：

```bash
shelfsignal collect \
  --workspace "$SHELFSIGNAL_WORKSPACE" \
  --auth fresh \
  --run-id 20260813T090000Z-daily
```

每次运行固定检查每个公众号最新 3 篇。首次运行最多交付每账号 3 篇；后续运行通过本地台账跳过已经保存的文章，只把当前窗口内的新文章写入简报。如果微信读书的文章列表暂时不可用，系统会保留仍可读取的最新一篇，并在简报中明确提示覆盖范围。

`fresh` 会在每个新 run 开始时要求扫码一次。同一个 run 如果中断，使用原 run ID 和相同认证策略重试即可；已完成的文章 checkpoint 和该 run 的授权状态会被复用。只有明确希望沿用可复用浏览器会话时，才使用 `--auth reuse`。

采集完成后，系统已经生成 cards、manifest、可见遗漏记录和 briefing。不要在正常成功后再次运行 `prepare-briefing`。已完成 run 不允许重复采集；下一次简报应使用新的 run ID。`prepare-briefing` 只用于恢复符合条件的未完成运行。

如需先查看书架中的公众号，可使用一个已知 run ID：

```bash
shelfsignal list-accounts \
  --workspace "$SHELFSIGNAL_WORKSPACE" \
  --auth fresh \
  --run-id 20260803T090000Z-canary
```

## 日常使用流程

向已安装的 ShelfSignal Skill 提出“生成微信公众号简报”之类的请求，并在需要时提供初始化后的 workspace 路径。Skill 会完成采集，为每篇新文章生成摘要和关键要点，并在呈现前校验简报结构和文章绑定信息。

阅读简报后，直接告诉 Agent 感兴趣的序号或标题。Agent 会定位该文章已经保存在本地的 `source.md` 和可选 `ocr.md`，继续做解释、比较、提炼或讨论。每篇简报也保留原文链接，方便在微信中获得原生阅读体验。

需要单独检查简报完整性时，可运行：

```bash
shelfsignal validate-briefing \
  --workspace "$SHELFSIGNAL_WORKSPACE" \
  "$SHELFSIGNAL_WORKSPACE/briefings/20260803T090000Z-daily.md"
```

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

ShelfSignal 采用 local-first 设计。采集的文章、图片、浏览器状态、简报和 SQLite 台账都保留在私有运行目录。Apple Vision OCR 完全在本机执行。项目不包含遥测、LLM 服务商 API、远程 OCR 或云数据库。

摘要和后续分析由用户当前使用的本地 Agent 完成，其数据边界取决于该宿主自身的配置。远端文章内容始终被视为不可信数据，不会被当作 Agent 指令执行。

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

## 许可证

ShelfSignal for WeChat 采用 [MIT License](LICENSE) 发布。

微信、WeChat、微信读书及其 Logo 是相关权利人的商标或品牌资产，仅用于说明本项目所连接的服务。本项目不代表腾讯、微信或微信读书，也未获得其官方背书。

制作 release candidate 时，需要构建 wheel 与 sdist，并显式运行针对发布产物的公开检查：

```bash
python -m build
SHELFSIGNAL_REQUIRE_DIST=1 python -m pytest -q tests/test_public_repository.py
```
