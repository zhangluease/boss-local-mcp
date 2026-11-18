"""Local MCP invocation history and per-batch filtering reports."""

from contextlib import closing, contextmanager
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import uuid


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id():
    return uuid.uuid4().hex


class RunHistory:
    def __init__(self, directory):
        self.directory = Path(directory)

    @contextmanager
    def connection(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(self.directory / "run-history.sqlite3")) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, tool TEXT NOT NULL, started_at TEXT NOT NULL,
                completed_at TEXT, outcome TEXT, batch_id TEXT
            )""")
            db.execute("CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            yield db

    def start_call(self, call_id, tool):
        with self.connection() as db:
            db.execute("INSERT INTO calls (id, tool, started_at) VALUES (?, ?, ?)", (call_id, tool, timestamp()))

    def finish_call(self, call_id, outcome, batch_id):
        with self.connection() as db:
            db.execute("UPDATE calls SET completed_at=?, outcome=?, batch_id=? WHERE id=?",
                       (timestamp(), outcome, batch_id, call_id))

    def save_batch(self, batch):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO batches VALUES (?, ?)",
                       (batch["batch_id"], json.dumps(batch, ensure_ascii=False)))

    @staticmethod
    def _read(db, batch_id):
        row = db.execute("SELECT payload FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise ValueError("找不到这个筛选批次")
        return json.loads(row[0])

    def candidate_in_batch(self, batch_id, expect_id):
        with self.connection() as db:
            batch = self._read(db, batch_id)
        return bool(expect_id) and any(row["expect_id"] == expect_id for row in batch["rows"])

    def record_detail(self, batch_id, expect_id, status, opened, identity_check="", code=""):
        with self._candidate_record(batch_id, expect_id) as row:
            record_detail_attempt(row, status, opened, identity_check, code)

    def record_follow_up(self, batch_id, expect_id, status, score, notes):
        with self._candidate_record(batch_id, expect_id) as row:
            row.setdefault("follow_ups", []).append({
                "at": timestamp(), "status": status, "score": score, "notes": notes,
            })

    @contextmanager
    def _candidate_record(self, batch_id, expect_id):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = self._read(db, batch_id)
            rows = [row for row in batch["rows"] if row["expect_id"] == expect_id]
            if not rows or not expect_id:
                raise ValueError("该候选人 ID 不在指定批次中")
            yield next((row for row in rows if row["disposition"] == "selected"), rows[0])
            db.execute("UPDATE batches SET payload=? WHERE id=?",
                       (json.dumps(batch, ensure_ascii=False), batch_id))

    def export(self, batch_id, format):
        if format not in ("md", "csv"):
            raise ValueError("报告格式须为 md 或 csv")
        with self.connection() as db:
            batch = self._read(db, batch_id)
        path = self.directory / "reports" / f"{batch['batch_id']}.{format}"
        path.parent.mkdir(exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".report-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8-sig" if format == "csv" else "utf-8", newline="") as output:
                if format == "csv":
                    _write_csv(batch, output)
                else:
                    _write_markdown(batch, output)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return {
            "batch_id": batch_id, "source": batch["source"], "entrypoint": batch["entrypoint"],
            "tool": batch["tool"], "status": batch["status"], "scans": batch["scans"],
            "row_count": len(batch["rows"]), "format": format, "path": str(path),
        }


def record_detail_attempt(row, status, opened, identity_check="", code=""):
    row["detail_status"] = status
    row["detail_attempts"].append({
        "at": timestamp(), "status": status, "target_detail_opened": opened,
        "identity_check": identity_check, "reason": code,
    })
    if opened:
        row["detail_opened"] = True


DISPOSITIONS = {
    "selected": "本轮返回，待人工复核", "mismatch": "卡片条件不符",
    "insufficient_evidence": "卡片证据不足，待核实", "duplicate": "已入库，本轮跳过",
    "result_limit": "达到本轮返回数量", "missing_id": "卡片缺少可定位的 ID",
}
DETAIL_STATUSES = {
    "not_requested": "本轮未请求", "not_attempted": "未尝试",
    "opened": "已核对详情", "failed": "查看失败",
    "not_visible": "卡片已不在当前列表", "identity_unverified": "详情身份无法核对",
}
DETAIL_REASONS = {
    "not_visible": "卡片已移出当前渲染范围", "identity_mismatch": "弹窗 ID 与目标不同",
    "identity_unverified": "弹窗未提供可核对的 ID", "ambiguous_id": "列表存在重复 ID",
    "dialog_missing": "未找到详情弹窗", "view_error": "详情读取异常",
}
STOP_REASONS = {
    "end_of_list": "页面明确提示已到底", "empty_results": "页面显示没有结果",
    "target_reached": "达到目标数量", "no_new_cards": "连续滚动无新增，未确认到底",
    "scan_timeout": "达到扫描时限", "scroll_limit": "达到滚动上限",
    "search_timeout": "等待新搜索结果超时", "search_error": "搜索出错",
    "list_unavailable": "列表在扫描过程中不可用", "verification": "页面要求用户验证",
}
RULES = {"expected_city": "期望城市", "total_experience": "总工作年限", "expected_salary": "期望薪资"}


def _conditions(scan):
    return "，".join(
        f"{label} {scan['filters'][field]}"
        for field, label in (("city", "期望城市"), ("experience", "总工作年限"), ("salary", "期望薪资"))
        if scan["filters"][field]
    ) or "未指定卡片条件"


def _report_rows(batch):
    for row in batch["rows"]:
        reasons = "；".join(
            f"{RULES[item['rule']]}要求 {item['expected']}，卡片为 {item['observed'] or '未提供'}"
            f"（{'不符' if item['result'] == 'mismatch' else '待核实'}）"
            for item in row["reasons"]
        )
        attempts = row["detail_attempts"]
        detail = DETAIL_STATUSES[row["detail_status"]]
        if attempts and attempts[-1]["reason"]:
            reason = attempts[-1]["reason"]
            detail += f"（{DETAIL_REASONS.get(reason, reason)}）"
        follow_ups = []
        for event in row.get("follow_ups", []):
            parts = [event["at"], event["status"], event["notes"]]
            if event["score"] is not None:
                parts.append(f"分数 {event['score']}")
            follow_ups.append(" · ".join(part for part in parts if part))
        yield row["scan_order"], [
            row["row_id"], row["keyword"], row["expect_id"], DISPOSITIONS[row["disposition"]],
            reasons, detail, "是" if row["detail_opened"] else "否",
            str(len(attempts)), row["card_evidence"]["expectCity"],
            row["card_evidence"]["experience"], row["card_evidence"]["salary"], "；".join(follow_ups),
        ]


COLUMNS = ["记录", "关键词", "候选人 ID", "卡片阶段结果", "判断依据", "详情状态",
           "本轮已核对详情", "详情尝试次数", "卡片期望城市", "卡片总工作年限", "卡片期望薪资", "后续复核记录"]


def _csv_cell(value):
    value = str(value)
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\n", "\r")):
        return "'" + value
    return value


def _write_csv(batch, output):
    writer = csv.writer(output)
    writer.writerow(["批次 ID", "执行入口", "列表来源", "本轮条件", "扫描停止原因", "滚动次数", *COLUMNS])
    metadata = (batch["batch_id"], batch["entrypoint"], batch["source"])
    rows = {}
    for order, row in _report_rows(batch):
        rows.setdefault(order, []).append(row)
    for scan in batch["scans"]:
        scope = (_conditions(scan), STOP_REASONS[scan["stop_reason"]], scan["scroll_count"])
        for row in rows.get(scan["order"], [["", scan["keyword"], *([""] * (len(COLUMNS) - 2))]]):
            writer.writerow([_csv_cell(value) for value in (*metadata, *scope, *row)])


def _md_cell(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\\", "\\\\").replace("|", "\\|").replace("\r", "").replace("\n", " ")
            .replace("[", "\\[").replace("]", "\\]").replace("`", "\\`"))


def _write_markdown(batch, output):
    output.write(f"# 候选人过滤记录\n\n批次 `{batch['batch_id']}` · {batch['entrypoint']} · {batch['source']}\n\n")
    output.write(f"开始时间 {batch['started_at']} · 调用 `{batch['tool']}`\n\n")
    output.write("记录范围为本轮读到的卡片。总工作年限不用于推断某个领域的经验。\n\n")
    output.write("## 扫描范围\n\n")
    for scan in batch["scans"]:
        output.write(f"- {_md_cell(scan['keyword'])} · {_md_cell(_conditions(scan))} · 滚动 {scan['scroll_count']} 次 · "
                     f"记录 {scan['cards_observed']} 条 · {STOP_REASONS[scan['stop_reason']]}\n")
        if scan.get("view_stop_reason") == "verification":
            output.write("  详情查阅因页面要求用户验证而停止。\n")
    output.write("\n## 逐条记录\n\n")
    for values in (COLUMNS, ["---"] * len(COLUMNS)):
        output.write("| " + " | ".join(values) + " |\n")
    for _, row in _report_rows(batch):
        output.write("| " + " | ".join(map(_md_cell, row)) + " |\n")
