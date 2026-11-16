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


def filter_loaded_candidates(candidates, city="", experience="", salary=""):
    requested_years = years_range(experience) if experience and experience != "经验不限" else None
    requested_salary = salary_range(salary) if salary and salary != "不限" else None
    if experience and experience != "经验不限" and requested_years is None:
        raise ValueError("experience 格式应为 3-5年、5年以上等")
    if salary and salary != "不限" and requested_salary is None:
        raise ValueError("salary 格式应为 20-30K 等带 K 单位的薪资区间")
    result = []
    for candidate in candidates:
        if city and city.rstrip("市") not in str(candidate.get("expectCity", "")):
            continue
        if requested_years:
            actual = years_range(candidate.get("experience"))
            if actual is None or actual[0] > requested_years[1] or actual[1] < requested_years[0]:
                continue
        if requested_salary:
            actual = salary_range(candidate.get("salary"))
            if actual is None or actual[0] > requested_salary[1] or actual[1] < requested_salary[0]:
                continue
        result.append(candidate)
    return result
