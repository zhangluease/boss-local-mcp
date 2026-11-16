# boss-local-mcp

在本机 Chrome 中操作 BOSS 直聘招聘者后台的 MCP 服务：搜索候选人、查看简历、截图与本地 OCR、提取分享链接、保存候选人数据并导出报告。

本项目源自 [Snseam/boss-zhipin-mcp](https://github.com/Snseam/boss-zhipin-mcp)，基于提交 [`06a1a7d`](https://github.com/Snseam/boss-zhipin-mcp/commit/06a1a7d804aa80131a066bddc1879ac4bc72f841) 改造，以全新的 Git 历史独立维护。来源和改动见 [NOTICE.md](NOTICE.md)。

## 验证状态

2026-11-16，在 macOS、Python 3.14.10、Chrome 157 和 FastMCP 4.2.0 环境完成验收：

- 29 项自动化测试通过，覆盖全部 17 个 MCP 工具，包括后台恢复加载和延迟加载的搜索 iframe。
- 15 个非沟通工具通过已登录账号的端到端验收，使用独立验收数据库。
- 真实搜索与滚动加载请求 31 条、返回 31 条不重复记录；简历截图、分享链接和本地 OCR 均通过。
- 两个沟通工具在模拟页面验证了点击联系与自定义消息，未向真实候选人发送消息。

详细范围、结果与复测方法见 [功能验证记录](docs/validation.md)。

## 快速开始

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和 Google Chrome。本机开发默认使用 Python 3.14，uv 会按 `.python-version` 选择版本。

```bash
git clone https://github.com/zhangluease/boss-local-mcp.git
cd boss-local-mcp
uv sync --extra test --locked
```

macOS 二维码解码需要 zbar：

```bash
brew install zbar
cp .env.example .env
cp search_profile.example.yaml search_profile.yaml
uv run boss-local-mcp doctor
uv run boss-local-mcp browser
```

在打开的专用 Chrome 中扫码登录**招聘者账号**。再次运行 `browser` 会打开现有窗口并检查登录状态，不会循环刷新登录页。

macOS 的本地 OCR 使用系统 Apple Vision。首次使用会编译随项目提供的 Swift 助手，需要 Xcode Command Line Tools。其他平台可使用已安装的 Tesseract；中文识别需配置对应语言数据及 `BOSS_OCR_LANG`。

## 接入 Codex

在项目根目录执行：

```bash
codex mcp add boss-local-mcp \
  --env NO_PROXY=localhost,127.0.0.1,::1 \
  -- "$PWD/.venv/bin/python" "$PWD/server.py"
```

配置后检查：

```bash
codex mcp get boss-local-mcp
uv run boss-local-mcp check
```

若客户端尚未显示新工具，重新加载 MCP 服务或重启客户端。

macOS 也提供三个可双击的入口：`open-boss.command` 打开浏览器，`test-mcp.command` 检查 MCP，`start-mcp.command` 启动 stdio 服务。

## 工具与使用顺序

通常先调用 `boss_login`，再搜索，把返回的 `index` 或 `expectId` 用于查看简历。需要 OCR 时传入 `include_ocr=true`；识别在本机执行，结果随 `ocr_text` 返回。

| 工具 | 用途 |
| --- | --- |
| `boss_login` | 检查登录；未登录时交给用户扫码 |
| `boss_search_candidates` | 单关键词搜索、加载、去重、入库 |
| `boss_multi_search` | 多关键词搜索；可自动查看新简历并获取链接 |
| `boss_view_by_index` | 按当前结果索引查看、截图、提取分享链接；支持本地 OCR |
| `boss_view_by_expect_id` | 按当前结果中的候选人 ID 查看；支持本地 OCR |
| `boss_view_candidate` | 按链接查看；已记录的分享链接优先复用当前搜索页 |
| `boss_evaluate_candidate` | 按传入岗位要求进行可解释的关键词匹配 |
| `boss_query_db` | 按状态、来源关键词、日期、有无链接查询 |
| `boss_update_candidate` | 更新本地备注、状态、分数、链接 |
| `boss_filter_and_score` | 按显式条件过滤并进行岗位关键词匹配 |
| `boss_export_report` | 返回含表格和可选详情的 Markdown 报告 |
| `boss_pipeline_status` | 查看统计与恢复建议 |
| `boss_clear_dedup` | 按 ID、状态、日期清理记录；无参数清空当前数据库 |
| `boss_greet_by_index` | 在当前搜索结果中发起沟通，可带自定义消息 |
| `boss_send_greeting` | 通过候选人页面链接发起沟通 |
| `boss_debug_page` | 读取当前 DOM 结构，辅助排查页面变化 |
| `boss_reload` | 重载模块并断开旧连接，保留 Chrome 窗口 |

单次搜索的 MCP 参数示例：

```json
{"keyword":"Python","count":10}
```

查看当前第一条简历并做本地 OCR：

```json
{"index":0,"include_ocr":true}
```

`count` 范围为 1–300。`city`、`experience`、`salary` 对已加载卡片进行本地过滤；它们不是对网站全部候选人的服务器筛选。例：`city="北京"`、`experience="3-5年"`、`salary="20-30K"`。缺少必要字段的卡片不会通过对应筛选，返回数量可能小于 `count`。

搜索默认去重，因此重复搜索可能只返回 `_stats`，候选人仍保存在数据库中。查看简历使用的是页面原始索引，而非去重后列表的位置。

## 配置

`.env` 控制本机运行环境，`search_profile.yaml` 控制岗位、搜索关键词和匹配条件。模板中的岗位只是示例，应替换为实际需求。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BOSS_CDP_URL` | `http://localhost:9222` | 本机 Chrome 调试入口 |
| `BOSS_CHROME_PATH` | 按系统查找 | Chrome 可执行文件路径 |
| `BOSS_CHROME_PROFILE_DIR` | `data/chrome-profile` | 专用 Chrome 用户目录，可复用已有专用目录 |
| `BOSS_DATA_DIR` | 项目下 `data` | 数据库、Cookie、OCR 缓存目录 |
| `BOSS_PROFILE_FILE` | 项目下 `search_profile.yaml` | 岗位配置文件 |
| `BOSS_MIN_DELAY` / `BOSS_MAX_DELAY` | `2` / `5` | 页面操作间的等待范围，单位秒 |
| `BOSS_OCR_LANG` | `eng` | Tesseract 语言，如 `chi_sim+eng` |

匹配分数使用岗位领域、技能和加分关键词，供人工复核；不使用年龄、性别等人口属性。薪资上限和求职状态排除项只有显式配置时才生效。简易关键词匹配不等同于模型语义评估。

本地 OCR 不需要 API key。上游云端 OCR 能力保留为可选 Python 接口：安装 `cloud-ocr` 扩展、配置 `ANTHROPIC_API_KEY` 和 `BOSS_OCR_MODEL` 后，显式选择 `ocr_image(..., engine="anthropic")` 才会调用；MCP 的简历 OCR 默认使用本地引擎。

候选人数据库和 Cookie 位于 `data`，简历截图保存在系统临时目录 `boss-recruiter-screenshots`，这些运行数据不纳入 Git。

## 页面关闭问题的处理

排查时捕获到 BOSS 页面脚本主动调用 `window.close()`；页面在未点击登录时也会关闭。断开旧调试连接后可正常扫码。BOSS 的[官方帮助](https://www.zhipin.com/faq/web/13/)要求关闭调试窗口。

本项目使用按需 CDP 命令，不订阅运行时控制台事件。新连接层已通过真实搜索、截图、分享链接、OCR 和批量操作验收。浏览器操作串行执行，点击前检查目标是否被弹窗遮挡，防止把点击落到其他控件上。

网站更新、账号权限、登录失效和安全验证仍可能影响页面操作；工具会返回具体结果，遇到验证码时由用户在浏览器中完成。

## 开发与复测

```bash
uv sync --extra test --locked
uv run pytest -q
uv build --wheel
```

自动化测试启动独立的无界面 Chrome，访问本地模拟招聘页面，覆盖全部 17 个工具、消息发送结果、二维码、本地 OCR、并发操作、数据库和 stdio/HTTP 协议。不会在真实 BOSS 页面执行沟通操作。

GitHub Actions 在 macOS 15 / Python 3.14 和 Ubuntu / Python 3.12 上运行同一套测试及 wheel 构建，云端不使用真实账号。

已登录账号的验收命令：

```bash
uv run python scripts/live_smoke.py --keyword Python --secondary SQL --count 2
```

该命令验证非沟通工具，使用独立的 `data/live-acceptance` 数据库，并清理其中的测试记录。摘要保存在 `data/live-acceptance/summary.json`。

可选 HTTP 服务仅绑定本机：

```bash
uv run python server.py --transport http --port 8090
```

MCP 地址为 `http://127.0.0.1:8090/mcp`。

## 项目结构

```text
browser.py          Chrome 生命周期与登录检查
cdp.py              基础 CDP、DOM、iframe、输入、截图
scraper.py          搜索、简历、分享、沟通流程
candidate_db.py     本地 JSON 数据库
filters.py          已加载卡片的显式筛选
ranking.py          岗位关键词匹配
evaluator.py        传入简历与岗位要求的关键词评估
ocr.py              本地 OCR 与可选云端接口
qr.py               二维码解码及 zbar 路径适配
server.py           17 个 MCP 工具
cli.py              启动、浏览器、检查、自检命令
boss_local_assets/  Apple Vision OCR 源码
tests/              本地模拟页面与回归测试
scripts/            安装与真实账号验收脚本
docs/               功能验证记录
```

## 许可证

[MIT](LICENSE)。感谢原项目及其贡献者，详细归属见 [NOTICE.md](NOTICE.md)。
