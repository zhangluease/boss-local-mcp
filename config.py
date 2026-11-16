"""Configuration for boss-recruiter-mcp."""

import os
from pathlib import Path

from dotenv import load_dotenv

import yaml

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)
DATA_DIR = Path(os.getenv("BOSS_DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.chmod(0o700)
CHROME_PROFILE_DIR = os.getenv("BOSS_CHROME_PROFILE_DIR", str(DATA_DIR / "chrome-profile"))

# BOSS 直聘
BOSS_BASE_URL = "https://www.zhipin.com"
BOSS_SEARCH_URL = "https://www.zhipin.com/web/chat/search"

# Cookie 持久化
COOKIES_DIR = str(DATA_DIR / "cookies")
COOKIES_FILE = os.path.join(COOKIES_DIR, "boss_cookies.json")

# 去重记录（旧版，迁移用）
DEDUP_FILE = str(DATA_DIR / "seen_candidates.json")

# 候选人数据库（新版，替代去重池）
CANDIDATE_DB_FILE = str(DATA_DIR / "candidates_db.json")

# 浏览器配置
BROWSER_HEADLESS = False  # 首次登录需要 GUI
SLOW_MO = 100  # 毫秒，模拟人类操作速度
MIN_DELAY = float(os.getenv("BOSS_MIN_DELAY", "2.0"))
MAX_DELAY = float(os.getenv("BOSS_MAX_DELAY", "5.0"))
if MIN_DELAY < 0 or MAX_DELAY < MIN_DELAY:
    raise ValueError("BOSS_MIN_DELAY / BOSS_MAX_DELAY 配置无效")
MAX_CANDIDATES_PER_SESSION = 30  # 单次会话最大抓取数

# AI 评估
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
EVAL_MODEL = os.getenv("BOSS_OCR_MODEL", "")

# 搜索配置（从 search_profile.yaml 加载）
PROFILE_FILE = os.getenv("BOSS_PROFILE_FILE", str(ROOT / "search_profile.yaml"))


def load_profile() -> dict:
    """Load search profile from YAML config file."""
    if os.path.exists(PROFILE_FILE):
        with open(PROFILE_FILE, "r", encoding="utf-8") as f:
            profile = yaml.safe_load(f) or {}
            if not isinstance(profile, dict):
                raise ValueError("search_profile.yaml 顶层必须是对象")
            return profile
    return {}


PROFILE = load_profile()

# 从 YAML 动态生成默认 JD
_job = PROFILE.get("job", {})
_company = PROFILE.get("company", {})
_reqs = PROFILE.get("requirements", {})

_must = "\n".join("- " + r for r in _reqs.get("must_have", []))
_nice = "\n".join("- " + r for r in _reqs.get("nice_to_have", []))

DEFAULT_JD = f"""岗位：{_job.get('title', '')}
公司：{_company.get('name', '')}（{_company.get('description', '')}）
要求：
{_must}
加分项：
{_nice}
薪资：{_job.get('salary', '')}
工作地：{_job.get('city', '')}
""" if PROFILE else ""
