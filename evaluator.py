"""Candidate evaluation — keyword-based scoring."""

import re

from config import DEFAULT_JD, PROFILE


class CandidateEvaluator:
    """Evaluates candidates against job requirements using keyword matching.

    For full AI evaluation, use Claude directly in the conversation
    and write scores back via boss_update_candidate.
    """

    def evaluate(self, resume: dict, job_requirements: str = "") -> dict:
        """Evaluate a candidate's resume against job requirements.

        Returns:
            {score: 0-100, strengths: [], weaknesses: [], recommendation: str, summary: str}
        """
        resume_text = self._format_resume(resume).lower()
        score = 50
        strengths = []
        weaknesses = []

        requirements = job_requirements.strip() or DEFAULT_JD
        configured = PROFILE.get("scoring", {})
        positive_keywords = list(dict.fromkeys(
            re.findall(r"[A-Za-z][A-Za-z0-9+#.]*|[\u4e00-\u9fff]{2,12}", requirements)
            + configured.get("tech_keywords", []) + configured.get("domain_keywords", [])
        ))
        stopwords = {"岗位", "公司", "要求", "加分项", "薪资", "工作地", "面议", "and", "or", "the", "with"}
        positive_keywords = [keyword for keyword in positive_keywords if keyword.lower() not in stopwords]

        for kw in positive_keywords:
            if kw.lower() in resume_text:
                score += max(1, 50 // max(1, len(positive_keywords)))
                strengths.append(f"包含关键词: {kw}")

            else:
                weaknesses.append(f"未匹配关键词: {kw}")

        score = max(0, min(100, score))
        recommendation = "yes" if score >= 70 else "maybe" if score >= 50 else "no"

        return {
            "score": score,
            "strengths": strengths or ["需要人工评估"],
            "weaknesses": weaknesses or ["无明显风险"],
            "recommendation": recommendation,
            "summary": f"关键词匹配分数 {score}/100（简易评估，建议在对话中让 Claude 详细评估）",
            "method": "keyword_match",
            "requirements_used": requirements,
        }

    def _format_resume(self, resume: dict) -> str:
        """Format resume dict into readable text."""
        parts = []
        if resume.get("education"):
            edu = resume["education"]
            if isinstance(edu, list):
                parts.append(f"教育：{'; '.join(edu)}")
            else:
                parts.append(f"教育：{edu}")
        if resume.get("skills"):
            parts.append(f"技能：{', '.join(resume['skills'])}")
        if resume.get("work_history"):
            parts.append("工作经历：")
            for w in resume["work_history"]:
                parts.append(f"  - {w}")
        if resume.get("project_experience"):
            parts.append("项目经验：")
            for p in resume["project_experience"]:
                parts.append(f"  - {p}")
        if resume.get("self_description"):
            parts.append(f"自我描述：{resume['self_description']}")
        full_text = resume.get("full_text") or resume.get("fullText")
        if full_text and not resume.get("work_history"):
            parts.append(f"简历全文：{full_text[:2000]}")
        return "\n".join(parts)
