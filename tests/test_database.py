from candidate_db import CandidateDB
from ranking import rank_candidates, salary_max_k
import pytest


def test_persistence_dedup_and_copy_isolation(tmp_path):
    path = str(tmp_path / "nested" / "db.json")
    database = CandidateDB(path)
    assert not database.add({"expectId": None})
    assert database.bulk_add([{"expectId": 1, "name": "测试候选人", "skills": ["Python"]}, {"expectId": "1"}], source_keyword="开发") == (1, 1)
    assert CandidateDB(path).get("1")["expectId"] == "1"
    assert CandidateDB(path).get("1")["name"] == "测试候选人"
    assert CandidateDB(path).get("1")["source_keyword"] == "开发"
    copy = database.query()[0]
    copy["skills"].append("SQL")
    assert database.get("1")["skills"] == ["Python"]
    assert database.query(limit=0) == []
    with pytest.raises(ValueError):
        database.query(limit=-1)
    assert database.update("1", status="viewed", score=70)
    assert len(database.query(status="viewed", has_share_url=False)) == 1
    assert database.stats()["total"] == 1
    with pytest.raises(ValueError):
        database.remove_before_date("not-a-date")
    assert database.remove_before_date("2099-01-01") == 1
    assert database.clear_all() == 0


def test_salary_parser_and_no_demographic_filtering(tmp_path):
    assert salary_max_k("15-20K·24薪") == 20
    assert salary_max_k("20K-30K") == 30
    assert salary_max_k("薪资面议") is None
    database = CandidateDB(str(tmp_path / "db.json"))
    for identifier, age in [("young", "27岁"), ("older", "55岁")]:
        database.add({"expectId": identifier, "age": age, "skills": ["Python"]})
    result = rank_candidates(database, {"filter": {"max_age": 35}, "scoring": {"tech_keywords": ["Python"]}}, 2)
    assert result[0]["shortlisted"] == 2
    assert result[1]["score"] == result[2]["score"]
    assert all(not any(key.startswith("_") for key in candidate) for candidate in database.query())


def test_legacy_migration(tmp_path):
    old = tmp_path / "old.json"
    old.write_text('{"ids":["甲","乙"]}', encoding="utf-8")
    database = CandidateDB(str(tmp_path / "new.json"), str(old))
    assert database.stats()["by_status"] == {"legacy": 2}
    assert database.has("甲") and database.has("乙")
    assert old.with_suffix(".json.bak").exists()
