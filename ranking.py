"""Explainable matching of job-related keywords; no demographic scoring."""

import re


def salary_max_k(text):
    text = str(text or "").replace(" ", "")
    match = re.search(r"(\d+(?:\.\d+)?)(?:[Kk])?[-~—至](\d+(?:\.\d+)?)[Kk]", text)
    if match:
        return max(float(match[1]), float(match[2]))
    match = re.search(r"(\d+(?:\.\d+)?)[Kk]", text)
    return float(match[1]) if match else None


def rank_candidates(database, profile, top_n):
    if not 1 <= top_n <= 1000:
        raise ValueError("top_n 必须在 1 到 1000 之间")
    filters = profile.get("filter", {})
    config = profile.get("scoring", {})
    maximum_salary = filters.get("max_salary_k")
    excluded_statuses = filters.get("exclude_status", [])
    groups = {
        "domain": config.get("domain_keywords", []),
        "tech": config.get("tech_keywords", []),
        "bonus": config.get("bonus_keywords", []),
    }
    candidates = [candidate for candidate in database.query(limit=100000) if candidate.get("status") != "legacy"]
    filtered = {"salary": 0, "status": 0}
    passed = []
    for candidate in candidates:
        salary = salary_max_k(candidate.get("salary"))
        if maximum_salary is not None and salary is not None and salary > maximum_salary:
            filtered["salary"] += 1
            continue
        if any(status in candidate.get("jobStatus", "") for status in excluded_statuses):
            filtered["status"] += 1
            continue
        text = " ".join([candidate.get("fullText", ""), candidate.get("title", ""), " ".join(candidate.get("skills", []))]).casefold()
        hits = {group: [keyword for keyword in words if keyword.casefold() in text] for group, words in groups.items()}
        score = min(100, 50 + min(24, len(hits["domain"]) * 8) + min(24, len(hits["tech"]) * 6) + min(10, len(hits["bonus"]) * 5))
        passed.append({**candidate, "score": score, "domain_hits": hits["domain"], "tech_hits": hits["tech"], "bonus_hits": hits["bonus"]})
    passed.sort(key=lambda candidate: (-candidate["score"], candidate["expectId"]))
    top = passed[:top_n]
    for rank, candidate in enumerate(top, 1):
        candidate["rank"] = rank
        database.update(candidate["expectId"], score=candidate["score"], status="shortlisted")
    return [{"_stats": True, "total_candidates": len(candidates), "filtered_out": filtered,
             "passed_filter": len(passed), "shortlisted": len(top)}] + top
