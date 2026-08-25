"""Conservative resume-import helpers.

Importing a document should give the candidate a reviewable draft, not silently
overwrite profile facts or invent structured work history from loose text.
"""

from __future__ import annotations

import re
from typing import Any

from ats_engine import extract_skill_mentions


EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d .()\-]{7,}\d)(?!\w)")
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
HEADING_RE = re.compile(
    r"^(professional summary|summary|profile|objective|skills|technical skills|core skills|experience|work experience|professional experience|employment history|education|certifications?|projects?)\s*:?$",
    re.IGNORECASE,
)


def clean(value: Any, limit: int = 3000) -> str:
    text = str(value or "").replace("\x00", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()[:limit]


def split_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {"header": []}
    current = "header"
    for raw_line in str(text or "").replace("\r", "").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        match = HEADING_RE.match(line)
        if match:
            current = match.group(1).casefold()
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


def likely_name(lines: list[str]) -> str:
    for line in lines[:5]:
        value = clean(line, 100)
        if not value or EMAIL_RE.search(value) or URL_RE.search(value) or PHONE_RE.search(value):
            continue
        if len(value.split()) <= 5 and not any(char.isdigit() for char in value):
            return value.title() if value.isupper() else value
    return ""


def list_skills(sections: dict[str, list[str]], text: str) -> list[str]:
    skills: list[str] = []
    for item in extract_skill_mentions(text):
        if item["skill"] not in skills:
            skills.append(item["skill"])
    for key in ("skills", "technical skills", "core skills"):
        for line in sections.get(key, []):
            for item in re.split(r"[,|;•·]", line):
                item = clean(item, 80)
                if item and len(item) <= 50 and item not in skills:
                    skills.append(item)
    return skills[:40]


def import_resume_text(text: str) -> dict[str, Any]:
    """Return a reviewable partial profile plus import notes."""
    original = str(text or "")[:30000]
    sections = split_sections(original)
    header = sections.get("header", [])
    emails = EMAIL_RE.findall(original)
    phones = PHONE_RE.findall(original)
    urls = URL_RE.findall(original)
    linkedin = next((url.rstrip(".,;") for url in urls if "linkedin.com" in url.casefold()), "")
    portfolio = next((url.rstrip(".,;") for url in urls if "linkedin.com" not in url.casefold()), "")
    summary_lines = sections.get("professional summary") or sections.get("summary") or sections.get("profile") or sections.get("objective") or []
    certifications = sections.get("certifications") or sections.get("certification") or []
    profile = {
        "full_name": likely_name(header),
        "headline": "",
        "email": emails[0] if emails else "",
        "phone": clean(phones[0], 60) if phones else "",
        "location": "",
        "linkedin_url": linkedin,
        "portfolio_url": portfolio,
        "summary": clean(" ".join(summary_lines), 1200),
        "skills": list_skills(sections, original),
        "experience": [],
        "education": [],
        "certifications": [clean(value, 160) for value in certifications[:20] if clean(value, 160)],
        "projects": [],
        "is_starter_template": False,
    }
    notes = [
        "Imported contact details, summary, recognised QA skills and certifications where readable.",
        "Review every imported field and add factual experience, education and metrics before saving or exporting.",
    ]
    if not profile["full_name"]:
        notes.append("A clear full name was not detected; add it manually.")
    if not profile["skills"]:
        notes.append("No skills were confidently detected; add your verified tools manually.")
    return {"profile": profile, "notes": notes, "raw_text": original}
