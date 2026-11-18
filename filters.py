"""Apply explicitly requested filters to the cards loaded from the website."""

import math
import re


def years_range(value):
    value = str(value or "")
    numbers = [float(number) for number in re.findall(r"\d+(?:\.\d+)?", value)]
    if "应届" in value:
        return 0, 0
    if not numbers:
        return None
    if "以上" in value:
        return numbers[0], math.inf
    if "以内" in value or "以下" in value:
        return 0, numbers[0]
    return min(numbers), max(numbers)


def salary_range(value):
    value = str(value or "").replace(" ", "")
    match = re.search(r"(\d+(?:\.\d+)?)[Kk]?[-~—至](\d+(?:\.\d+)?)[Kk]", value)
    if match:
        return min(float(match[1]), float(match[2])), max(float(match[1]), float(match[2]))
    match = re.search(r"(\d+(?:\.\d+)?)[Kk]", value)
    return (float(match[1]), float(match[1])) if match else None


def card_filter(city="", experience="", salary=""):
    city = city.strip().removesuffix("市")
    experience, salary = experience.strip(), salary.strip()
    requested_years = years_range(experience) if experience not in ("", "经验不限") else None
    requested_salary = salary_range(salary) if salary not in ("", "不限") else None
    if experience and experience != "经验不限" and requested_years is None:
        raise ValueError("experience 格式应为 3-5年、5年以上等")
    if salary and salary != "不限" and requested_salary is None:
        raise ValueError("salary 格式应为 20-30K 等带 K 单位的薪资区间")
    def evaluate(candidate):
        reasons = []
        if city and city != "不限":
            actual = str(candidate.get("expectCity") or "").strip()
            if not actual or city not in actual:
                reasons.append({"rule": "expected_city", "expected": city, "observed": actual,
                                "result": "mismatch" if actual else "insufficient_evidence"})
        for field, rule, value, expected, parse in (
            ("experience", "total_experience", experience, requested_years, years_range),
            ("salary", "expected_salary", salary, requested_salary, salary_range),
        ):
            if expected is None:
                continue
            observed = str(candidate.get(field) or "").strip()
            actual = parse(observed)
            if actual is None or actual[0] > expected[1] or actual[1] < expected[0]:
                reasons.append({"rule": rule, "expected": value, "observed": observed,
                                "result": "mismatch" if actual else "insufficient_evidence"})
        return reasons

    return evaluate


def filter_loaded_candidates(candidates, city="", experience="", salary=""):
    evaluate = card_filter(city, experience, salary)
    return [candidate for candidate in candidates if not evaluate(candidate)]
