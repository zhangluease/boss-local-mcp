# 功能验证记录

日期：2026-11-16，Asia/Shanghai。

## 环境与结论

本机 macOS、Python 3.14.10、Chrome 157.0.8103.78、FastMCP 4.2.0。29 项自动化测试通过，涵盖 17 个 MCP 工具。真实已登录招聘者环境的非沟通工具验收通过；沟通工具在本地模拟页面测试，真实发送次数为 0。

## 工具覆盖

| 工具 | 本地模拟页面 / 数据 | 真实登录环境 |
| --- | --- | --- |
| `boss_login` | 通过 | 通过 |
| `boss_search_candidates` | 通过 | 通过；另验证滚动加载返回 31 条不重复记录 |
| `boss_multi_search` | 通过；去重和自动查看分支 | 通过 |
| `boss_view_by_index` | 通过；截图、二维码、本地 OCR | 通过 |
| `boss_view_by_expect_id` | 通过 | 通过 |
| `boss_view_candidate` | 通过；页面 URL 与已记录分享链接 | 通过；复用数据库中的分享链接 |
| `boss_evaluate_candidate` | 通过；岗位要求影响匹配结果 | 通过；验收使用合成技能文本 |
| `boss_query_db` | 通过 | 通过；独立验收库 |
| `boss_update_candidate` | 通过；状态、分数校验 | 通过；独立验收库 |
| `boss_filter_and_score` | 通过；无隐式年龄过滤 | 通过；独立验收库 |
| `boss_export_report` | 通过；岗位标题与详情 | 通过 |
| `boss_pipeline_status` | 通过 | 通过 |
| `boss_clear_dedup` | 通过；ID、状态、日期 | 通过；只清理独立验收库 |
| `boss_greet_by_index` | 通过；联系动作及自定义消息有实际页面断言 | 未向真实候选人执行 |
| `boss_send_greeting` | 通过；自定义消息及部分失败反馈 | 未向真实候选人执行 |
| `boss_debug_page` | 通过 | 通过 |
| `boss_reload` | 通过 | 通过；保留 Chrome 窗口 |

## 关键证据、发现与修复

| 证据 | 发现 | 修复位置 |
| --- | --- | --- |
| 页面在打开数秒后调用 `window.close()`，调用栈来自网站 `main.js` 的 `Bm()`；断开原连接后正常扫码 | 原接管方式与当前页面的调试检查不兼容 | `cdp.py`、`browser.py`：按需执行基础命令，不订阅控制台事件 |
| 隐藏导航元素也能触发原有登录成功判断 | 登录状态误判 | `browser.py`：可见性和招聘者工作台联合检查 |
| 已登录后台恢复时，首次数毫秒检查无导航，随后检查成功 | 尚未加载的后台被短暂误判为未登录 | `browser.py`：最多等待 5 秒检查后台 DOM 就绪，不刷新或跳转页面；回归用例确认文档保留 |
| 分享面板的关闭代码会删除其父节点，本地测试中连同页面主体一起消失 | 后续候选人 ID 查看失效 | `scraper.py`：关闭正常弹窗控件，限制移除范围 |
| 当前页面转发入口为 `.btn-coop-forward` | 旧 Canvas 点击坐标不足以覆盖当前 UI | `scraper.py`：优先使用真实转发入口，保留 Canvas 兼容路径 |
| 键盘输入后未产生有效的 Enter 字符事件 | 搜索可能保留旧列表 | `cdp.py`：补全 Enter 键事件；真实请求包含验收关键词 |
| macOS CI 在 iframe 出现后、文档加载完成前读取搜索框 | 慢速页面可能返回“无法找到搜索输入框” | `scraper.py`：等待可见搜索框；两个回归用例分别覆盖新打开和已有的延迟 iframe |
| 请求 3 条曾返回整页候选人 | 数量参数未生效 | `scraper.py`：参数范围校验与结果截取 |
| 带年终薪月数的 `15-20K·24薪` 被误读为 24K | 薪资解析混入薪月数 | `ranking.py`、`filters.py` |
| 发送失败仍可能写入 `greeted`，缺少消息输入框仍返回成功 | 沟通结果不可靠 | `server.py`、`scraper.py`：按结果更新状态，区分部分失败 |
| 不同岗位仍输出“北京 AI 产品经理” | 报告配置未生效 | `server.py`：按实际岗位生成标题 |
| 岗位要求参数没有参与简易评估 | 固定关键词导致匹配结果失真 | `evaluator.py`：使用传入岗位要求与配置关键词 |

另外验证了数据库原子写入、去重、查询副本隔离、旧去重记录迁移、点击遮挡检查、浏览器操作串行化、从其他工作目录启动 stdio 及本机 HTTP 传输。

GitHub Actions 配置 macOS 15 / Python 3.14 与 Ubuntu / Python 3.12，执行本地模拟测试和 wheel 构建。CI 保留 macOS Chrome 的 `.app` 应用包结构，并在 Linux 测试进程中使用适配 runner 的沙箱参数；这些配置不改变正式浏览器的启动参数。云端不持有真实账号资料。

## OCR

本地 Apple Vision 已识别真实简历截图。MCP 端到端验收中的一次输出为 482 字符。自动化测试使用合成图片和模拟简历，不将真实简历内容纳入测试文件。

可选 Anthropic 接口不属于默认执行路径，未进行付费云端 API 调用。macOS 以外平台依赖本机 Tesseract 安装情况；本机验证结果不等同于所有系统均已实测。

## 复测

```bash
uv sync --extra test --locked
uv run pytest -q
uv run boss-local-mcp check
```

真实账号验收：

```bash
uv run python scripts/live_smoke.py --keyword Python --secondary SQL --count 2
```

本机运行的摘要保存在被 Git 忽略的 `data/live-acceptance/summary.json`。真实候选人资料、Cookie、截图、账号配置和页面源码不随本仓库发布。

## 2026-11-17：固定版本浏览器安装

环境安装入口 `scripts/setup.sh` 和 `scripts/setup.ps1` 同时安装 Python 依赖和 Chrome for Testing，完整版本固定在 `boss_local_assets/browser.json`。默认安装目录为项目 `.browsers/157.0.8103.78/<平台>/`，登录资料继续使用独立的 `data/chrome-profile` 或已有显式配置。

本机已从 Google 官方下载并安装 mac-arm64 浏览器，程序文件版本为 `157.0.8103.78`，重复安装返回 `already_installed`，临时 ZIP 自动删除。47 项本地测试通过，覆盖安装失败清理、版本不符拒绝连接、宿主工作目录变化、平台选择、下载互斥及原有业务功能。另使用项目内 CfT 执行浏览器回归测试。

| 证据 | 发现 | 路径 |
| --- | --- | --- |
| 原实现默认查找系统 Chrome，已启动的调试端口可连接其他版本 | Python 依赖一致仍不能保证浏览器一致 | `browser_install.py`、`browser.py`、`cdp.py`：本地安装并校验文件版本和 CDP 版本 |
| Windows 没有 POSIX 的新会话语义 | 同一个启动参数不能表达两端相同的控制台行为 | `browser.py`：Windows 分离控制台及独立进程组；不声称解决宿主 Job Object 清理 |
| 从其他目录启动 Agent 时，工作目录会不同 | 相对路径不能依赖宿主当前目录 | `browser_install.py`：以项目根目录解析默认和显式相对路径 |
| Windows CI 使用默认 cp1252，中文模拟页面无法读取 | 文本读写不能假定系统默认编码为 UTF-8 | 模拟服务器、`candidate_db.py` 与 `ocr.py` 显式使用 UTF-8，并验证中文数据往返及 OCR 输出 |

CI 新增 Windows，并改为执行本项目安装入口，随后验证本地模拟页面；云端不登录真实账号。原有 2026-11-16 的真实账号记录仍按上述范围解读。

## 2026-11-17：扫码等待时释放连接

Windows 同事反馈，独立 PowerShell 执行 `python -m cli browser` 后扫码窗口可以保留。命令行入口原本会在返回前断开调试连接，MCP 未登录分支则会保留连接。本次将 MCP 等待扫码分支也改为短连接，并清理共享的浏览器及搜索器引用；发生登录导航异常时同样释放连接。

| 证据 | 发现 | 路径 |
| --- | --- | --- |
| CLI 能保留窗口，且其 finally 会断开连接 | MCP 等待扫码时的连接生命周期与 CLI 不同 | `server.py`：未登录时在返回前断开 CDP，保留 Chrome，并返回 `next_action: wait_for_user` |
| 回归测试断开后仍能找到原标签页，原文档标记仍在 | 释放连接未关闭或刷新扫码页 | `tests/test_tools.py`：模拟用户登录后重新连接、保存 Cookie 并继续搜索 |
| 导航异常后连接已关闭，共享引用为空 | 失败路径也不遗留调试连接 | `tests/test_tools.py`：登录导航异常用例 |

本机 49 项测试通过。测试使用本地模拟页面，不向真实候选人发送消息；当前证据不等同于确认 Windows + WorkBuddy 的全部关闭原因，仍需同事更新并重启 MCP 后复测真实扫码。
