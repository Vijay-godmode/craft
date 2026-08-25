"""Editable QA/Test Engineer starter profile.

The starter intentionally contains visible placeholders, never a fabricated
candidate identity. It gives first-time users an ATS-safe structure spanning
manual testing, automation, API testing, and SDET work.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


STARTER_PROFILE: dict[str, Any] = {
    "full_name": "Your Name",
    "headline": "QA / Test Engineer | Manual, Automation and API Testing",
    "email": "your.email@example.com",
    "phone": "+91 00000 00000",
    "location": "Your City, India / Open to Remote",
    "linkedin_url": "",
    "portfolio_url": "",
    "summary": (
        "QA / Test Engineer with hands-on experience across manual testing, test automation, "
        "API validation, regression coverage and Agile product delivery. Replace this starter "
        "summary with your genuine scope, tools and measurable outcomes."
    ),
    "skills": [
        "Manual Testing",
        "Test Case Design",
        "Regression Testing",
        "API Testing",
        "Postman",
        "SQL",
        "Selenium",
        "Test Automation",
        "Jira",
        "Agile",
        "Git",
        "CI/CD",
    ],
    "experience": [
        {
            "title": "QA / Test Engineer",
            "company": "Your most recent product company",
            "location": "Your City / Remote",
            "start_date": "Month YYYY",
            "end_date": "Present",
            "current": True,
            "bullets": [
                "Replace with a genuine outcome: designed and executed functional, regression and exploratory test scenarios for a product release.",
                "Replace with a genuine outcome: validated REST APIs and backend data using your actual tools and test data.",
                "Replace with a genuine outcome: collaborated with engineering and product teams to report, prioritise and verify defects.",
            ],
        }
    ],
    "education": [
        {
            "degree": "Your degree or qualification",
            "school": "Your university or institution",
            "location": "Your City",
            "graduation": "YYYY",
        }
    ],
    "certifications": [],
    "projects": [
        {
            "name": "Optional QA automation or API testing project",
            "url": "",
            "description": "Replace with a real project that demonstrates the tools and testing approach you used.",
            "bullets": ["Replace with a factual result, test suite, contribution or learning outcome."],
        }
    ],
    "is_starter_template": True,
    "template_name": "QA / Test Engineer Starter",
}


def qa_starter_profile() -> dict[str, Any]:
    """Return a fresh editable copy of the starter profile."""
    return deepcopy(STARTER_PROFILE)


def has_starter_placeholders(profile: dict[str, Any]) -> bool:
    """Avoid exporting a document that still looks like an unedited template."""
    placeholders = {
        "your name",
        "your.email@example.com",
        "your city, india / open to remote",
        "your most recent product company",
        "your university or institution",
    }
    values = {
        str(profile.get("full_name") or "").casefold(),
        str(profile.get("email") or "").casefold(),
        str(profile.get("location") or "").casefold(),
    }
    for entry in profile.get("experience") or []:
        if isinstance(entry, dict):
            values.add(str(entry.get("company") or "").casefold())
    for entry in profile.get("education") or []:
        if isinstance(entry, dict):
            values.add(str(entry.get("school") or "").casefold())
    if values & placeholders:
        return True

    # Replacing only a name or email must not make template bullets look like
    # real accomplishments. These phrases are deliberately visible in the
    # starter, so they are safe to identify before personalised export.
    def walk(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value.casefold()]
        if isinstance(value, dict):
            return [item for child in value.values() for item in walk(child)]
        if isinstance(value, list):
            return [item for child in value for item in walk(child)]
        return []

    template_markers = (
        "replace with",
        "your degree or qualification",
        "your city / remote",
        "month yyyy",
        "+91 00000 00000",
        "optional qa automation or api testing project",
    )
    return any(marker in text for text in walk(profile) for marker in template_markers)
