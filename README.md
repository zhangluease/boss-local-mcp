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

2026-11-17 新增项目本地 Chrome 固定版本安装及扫码等待时释放连接，本机 49 项自动化测试通过，包含安装、版本校验和原有业务回归。

## 快速开始

需要 [uv](https://docs.astral.sh/uv/getting-started/installation/)。uv 按 `.python-version` 管理 Python，安装入口同时安装 Python 依赖和项目专用的 **Chrome for Testing 157.0.8103.78**。

```bash
git clone https://github.com/zhangluease/boss-local-mcp.git
cd boss-local-mcp
```

Windows（PowerShell）：

```powershell
.\scripts\setup.ps1
```

macOS（二维码解码需要 zbar）：

```bash
brew install zbar
sh scripts/setup.sh
```

Ubuntu / Debian：

```bash
sudo apt-get install -y libzbar0 tesseract-ocr
sh scripts/setup.sh
```

安装后可按需复制配置，再打开扫码窗口；已有配置可继续使用。Windows：

```powershell
Copy-Item .env.example .env
Copy-Item search_profile.example.yaml search_profile.yaml
uv run boss-local-mcp browser
```

macOS / Linux：

```bash
cp .env.example .env
cp search_profile.example.yaml search_profile.yaml
uv run boss-local-mcp browser
```

在打开的专用 Chrome 中扫码登录**招聘者账号**。再次运行 `browser` 会打开现有窗口并检查登录状态，不会循环刷新登录页。

macOS 的本地 OCR 使用系统 Apple Vision。首次使用会编译随项目提供的 Swift 助手，需要 Xcode Command Line Tools。其他平台可使用已安装的 Tesseract；中文识别需配置对应语言数据及 `BOSS_OCR_LANG`。

## 项目浏览器的安装与版本

Chrome 是独立程序，`uv sync` 只安装 Python 包；`scripts/setup.sh` / `scripts/setup.ps1` 将这两个步骤合成一次安装。也可单独执行：

```bash
uv sync --locked
uv run --locked boss-local-mcp install-browser
uv run --locked boss-local-mcp doctor
```

完整版本号记录在 [browser.json](boss_local_assets/browser.json)，以已验证的 macOS 版本 `157.0.8103.78` 为基准，按系统架构下载 Google 官方 [Chrome for Testing](https://developer.chrome.com/docs/automation-and-testing/chrome-for-testing)。它提供固定版本的浏览器包，支持项目自行管理升级。

```text
boss-local-mcp/
  .venv/                         Python 依赖
  .browsers/157.0.8103.78/        固定版本的浏览器，按平台分目录
  data/chrome-profile/           招聘者登录资料
```

路径以项目根目录为准，WorkBuddy 的当前工作目录不会改变默认安装位置。`.browsers` 和 `data` 均被 Git 忽略；浏览器升级与登录资料分开管理。安装成功或失败都会清理本次下载的临时 ZIP；重复安装会校验并复用已安装的版本。

启动时使用项目浏览器，也会校验已有 CDP 连接报告的完整版本号。若调试端口上的 Chrome 版本不一致，返回明确错误并保留现有窗口。退出旧的 MCP 专用 Chrome 后重试，不会自动切换到系统里的其他版本。`BOSS_CHROME_PATH` 可用于显式指定同版本的浏览器。

`doctor` 显示实际程序路径、文件版本、要求版本和依赖状态；浏览器版本不符或二维码依赖缺失时返回非零退出码。安装锁提示存在其他任务时，请先确认是否仍在安装；异常退出遗留的 `.install-版本-平台.lock` 可在确认无安装进程后移除。

固定版本用于减少环境变量，不保证解决 WorkBuddy 在 Windows 上的进程清理或网站验证问题。Windows 启动使用独立进程组和分离控制台，这不等同于脱离宿主的 Job Object。固定版本需要通过更新清单及回归测试安排后续升级。

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

WorkBuddy 使用标准 stdio MCP 配置：Windows 的启动程序选择项目内 `.venv\Scripts\python.exe`，macOS / Linux 选择 `.venv/bin/python`，参数为项目 `server.py` 的绝对路径。业务操作调用 `boss_*` 工具；扫码阶段返回 `waiting_for_login` 和 `next_action: wait_for_user` 后等待用户完成。更新项目代码后重启 MCP 服务，以加载新的工具逻辑。

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
| `boss_export_filter_log` | 按搜索批次导出逐条过滤记录，支持 Markdown 和 CSV |
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

`city`、`experience`、`salary` 对已读取的卡片进行本地比对。这里的 `experience` 指总工作年限。卡片缺值或无法解析时，报告标记为「证据不足，待核实」。

搜索默认按符合卡片条件的人数停止。选择 `scan_mode="end"` 可以在同一查询中继续向下扫描；`count`（1–300）仍限制每个关键词的返回数量，`max_scrolls`（1–200，默认 40）和 90 秒扫描时限控制单次扫描。只有看到明确的页面结束提示才返回 `end_of_list`。连续无新增、达到上限、超时以及需要验证都有单独的停止原因。扫描过程中不会循环刷新页面。

搜索按 ID 去重，统计包含已观察卡片、新增候选人和卡片处理结果。虚拟列表可能移走先前读到的卡片，此时返回的 `index` 为空。查看简历优先使用 `expectId`。按 ID 查看时会核对弹窗的候选人 ID，无法核对则返回明确状态。

### 导出一次搜索的过滤记录

1. 调用 `boss_search_candidates` 或 `boss_multi_search`，从首条统计中取 `batch_id`。只读卡片时，批量搜索传入 `auto_view=false`。
2. 需要看详情时，调用 `boss_view_by_expect_id` 并传入该 `batch_id`。如需记录后续人工判断，调用 `boss_update_candidate`，同样传入批次 ID 及备注。
3. 调用 `boss_export_filter_log`，指定同一 `batch_id` 和 `format="md"` 或 `"csv"`。工具在 `data/reports/` 下写入文件并返回路径，可在查看详情或填写备注后重新导出。

报告包括批次来源、每个关键词的扫描停止原因、卡片条件与证据、详情状态和显式写入的复核备注。报告只覆盖本轮实际读取的卡片；关键词搜索与「推荐／精选」是不同的列表来源。

在 WorkBuddy 会话中调用 `boss_pipeline_status`，若返回 `entrypoint: mcp` 和 `call_id`，可确认该会话发起的调用已到达服务端。`boss-local-mcp check` 测试本机 stdio 调用。服务端把调用时间、工具名、调用状态和关联批次保存在本地 `data/run-history.sqlite3`。

## 配置

`.env` 控制本机运行环境，`search_profile.yaml` 控制岗位、搜索关键词和匹配条件。模板中的岗位只是示例，应替换为实际需求。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BOSS_CDP_URL` | `http://localhost:9222` | 本机 Chrome 调试入口 |
| `BOSS_BROWSER_DIR` | 项目下 `.browsers` | 固定版本浏览器的安装目录 |
| `BOSS_CHROME_PATH` | 项目浏览器 | 显式指定同版本 Chrome 的可执行文件；相对路径以项目根目录为准 |
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

`boss_login` 未登录时，会打开扫码页面，在返回前断开调试连接并保留 Chrome，响应中包含 `browser_connection: disconnected`。等待扫码期间没有本项目维持的后台调试连接，也不轮询或刷新。用户扫码完成后再次调用 `boss_login`，服务重新连接现有窗口、确认登录并保存 Cookie；登录成功后的业务操作照常使用连接。

给 WorkBuddy 的操作指令可使用：“只调用 boss-local-mcp 的 boss_login 一次。返回 waiting_for_login 后停止操作，等待我扫码完成；不要用其他浏览器工具接管、刷新或重新打开窗口。我确认完成后，再调用 boss_login。”

Windows 同事已反馈独立命令行打开的扫码窗口不会自动关闭；本次将 MCP 的扫码等待阶段与该方式对齐。此反馈没有确认全部关闭原因，实际 Windows + WorkBuddy 登录仍需要更新后复测。

网站更新、账号权限、登录失效和安全验证仍可能影响页面操作；工具会返回具体结果，遇到验证码时由用户在浏览器中完成。

## 开发与复测

```bash
uv sync --extra test --locked
uv run pytest -q
uv build --wheel
```

自动化测试启动独立的无界面 Chrome，访问本地模拟招聘页面，覆盖工具调用、扫描与报告、消息发送结果、二维码、本地 OCR、并发操作、数据库和 stdio/HTTP 协议。

GitHub Actions 在 macOS 15 / Python 3.14、Windows / Python 3.14 和 Ubuntu / Python 3.12 上安装清单指定的项目浏览器，运行模拟页面测试及 wheel 构建。云端不使用真实账号。

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
browser_install.py  固定版本浏览器的安装、定位与校验
cdp.py              基础 CDP、DOM、iframe、输入、截图
scraper.py          搜索、简历、分享、沟通流程
candidate_db.py     本地 JSON 数据库
filters.py          已加载卡片的显式筛选
ranking.py          岗位关键词匹配
run_history.py      MCP 调用记录与筛选批次报告
evaluator.py        传入简历与岗位要求的关键词评估
ocr.py              本地 OCR 与可选云端接口
qr.py               二维码解码及 zbar 路径适配
server.py           18 个 MCP 工具
cli.py              启动、浏览器、检查、自检命令
boss_local_assets/  浏览器版本清单、Apple Vision OCR 源码
tests/              本地模拟页面与回归测试
scripts/            安装与真实账号验收脚本
docs/               功能验证记录
```

## 许可证

[MIT](LICENSE)。感谢原项目及其贡献者，详细归属见 [NOTICE.md](NOTICE.md)。
