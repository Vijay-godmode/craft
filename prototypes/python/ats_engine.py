"""Explainable, lightweight job-requirement matching for QA resumes.

The engine deliberately uses small, inspectable phrase rules rather than a
downloaded model. It runs well on a mid-range laptop, shows evidence for each
match, and never adds a qualification the user has not supplied.
"""

from __future__ import annotations

from collections import OrderedDict
import html
import re
from typing import Any


# Canonical phrase -> aliases and category. The taxonomy is focused on QA/SDET
# work but includes the platforms and technologies commonly requested beside it.
QA_SKILLS: "OrderedDict[str, dict[str, Any]]" = OrderedDict(
    [
        ("Manual Testing", {"aliases": ["manual testing", "manual tester", "functional testing"], "category": "Testing"}),
        ("Test Automation", {"aliases": ["test automation", "automated testing", "automation testing", "automation framework"], "category": "Testing"}),
        ("Regression Testing", {"aliases": ["regression testing", "regression tests"], "category": "Testing"}),
        ("Smoke Testing", {"aliases": ["smoke testing", "smoke tests", "sanity testing"], "category": "Testing"}),
        ("Integration Testing", {"aliases": ["integration testing", "integration tests"], "category": "Testing"}),
        ("End-to-End Testing", {"aliases": ["end to end testing", "end-to-end testing", "e2e testing", "e2e tests"], "category": "Testing"}),
        ("API Testing", {"aliases": ["api testing", "api tests", "web service testing", "rest api testing"], "category": "Testing"}),
        ("Mobile Testing", {"aliases": ["mobile testing", "ios testing", "android testing"], "category": "Testing"}),
        ("Performance Testing", {"aliases": ["performance testing", "load testing", "stress testing"], "category": "Testing"}),
        ("Accessibility Testing", {"aliases": ["accessibility testing", "a11y testing", "wcag"], "category": "Testing"}),
        ("Exploratory Testing", {"aliases": ["exploratory testing"], "category": "Testing"}),
        ("UAT", {"aliases": ["user acceptance testing", "uat"], "category": "Testing"}),
        ("Selenium", {"aliases": ["selenium", "selenium webdriver"], "category": "Automation"}),
        ("Playwright", {"aliases": ["playwright"], "category": "Automation"}),
        ("Cypress", {"aliases": ["cypress"], "category": "Automation"}),
        ("Appium", {"aliases": ["appium"], "category": "Automation"}),
        ("Robot Framework", {"aliases": ["robot framework"], "category": "Automation"}),
        ("Cucumber", {"aliases": ["cucumber", "gherkin", "bdd"], "category": "Automation"}),
        ("TestNG", {"aliases": ["testng"], "category": "Automation"}),
        ("JUnit", {"aliases": ["junit"], "category": "Automation"}),
        ("pytest", {"aliases": ["pytest"], "category": "Automation"}),
        ("REST Assured", {"aliases": ["rest assured", "rest-assured"], "category": "Automation"}),
        ("Postman", {"aliases": ["postman"], "category": "Tools"}),
        ("SoapUI", {"aliases": ["soapui", "soap ui"], "category": "Tools"}),
        ("JMeter", {"aliases": ["jmeter", "apache jmeter"], "category": "Tools"}),
        ("k6", {"aliases": ["k6"], "category": "Tools"}),
        ("BrowserStack", {"aliases": ["browserstack"], "category": "Platforms"}),
        ("Sauce Labs", {"aliases": ["sauce labs"], "category": "Platforms"}),
        ("Allure", {"aliases": ["allure"], "category": "Tools"}),
        ("Java", {"aliases": ["java"], "category": "Language"}),
        ("Python", {"aliases": ["python"], "category": "Language"}),
        ("JavaScript", {"aliases": ["javascript", "node.js", "nodejs"], "category": "Language"}),
        ("TypeScript", {"aliases": ["typescript"], "category": "Language"}),
        ("C#", {"aliases": ["c#", "csharp", "c sharp"], "category": "Language"}),
        ("C++", {"aliases": ["c++", "cpp"], "category": "Language"}),
        ("SQL", {"aliases": ["sql"], "category": "Data"}),
        ("PostgreSQL", {"aliases": ["postgresql", "postgres"], "category": "Data"}),
        ("MySQL", {"aliases": ["mysql"], "category": "Data"}),
        ("MongoDB", {"aliases": ["mongodb", "mongo db"], "category": "Data"}),
        ("Oracle", {"aliases": ["oracle", "oracle database"], "category": "Data"}),
        ("GraphQL", {"aliases": ["graphql"], "category": "Data"}),
        ("SOAP", {"aliases": ["soap"], "category": "Data"}),
        ("Git", {"aliases": ["git"], "category": "Tools"}),
        ("GitHub", {"aliases": ["github"], "category": "Tools"}),
        ("GitLab", {"aliases": ["gitlab"], "category": "Tools"}),
        ("Jira", {"aliases": ["jira", "atlassian jira"], "category": "Tools"}),
        ("TestRail", {"aliases": ["testrail", "test rail"], "category": "Tools"}),
        ("Zephyr", {"aliases": ["zephyr"], "category": "Tools"}),
        ("Xray", {"aliases": ["xray", "xray jira"], "category": "Tools"}),
        ("CI/CD", {"aliases": ["ci/cd", "continuous integration", "continuous delivery", "continuous deployment"], "category": "Delivery"}),
        ("Jenkins", {"aliases": ["jenkins"], "category": "Delivery"}),
        ("GitHub Actions", {"aliases": ["github actions"], "category": "Delivery"}),
        ("GitLab CI", {"aliases": ["gitlab ci", "gitlab-ci"], "category": "Delivery"}),
        ("Azure DevOps", {"aliases": ["azure devops", "azure pipelines"], "category": "Delivery"}),
        ("CircleCI", {"aliases": ["circleci", "circle ci"], "category": "Delivery"}),
        ("Docker", {"aliases": ["docker", "containerization", "containers"], "category": "Delivery"}),
        ("Kubernetes", {"aliases": ["kubernetes", "k8s"], "category": "Delivery"}),
        ("AWS", {"aliases": ["aws", "amazon web services"], "category": "Cloud"}),
        ("Azure", {"aliases": ["microsoft azure", "azure cloud"], "category": "Cloud"}),
        ("Linux", {"aliases": ["linux", "unix"], "category": "Platforms"}),
        ("Microservices", {"aliases": ["microservices", "microservice"], "category": "Architecture"}),
        ("Kafka", {"aliases": ["kafka", "apache kafka"], "category": "Architecture"}),
        ("Agile", {"aliases": ["agile", "scrum", "kanban"], "category": "Ways of working"}),
        ("Defect Management", {"aliases": ["defect management", "bug tracking", "defect tracking", "bug reporting"], "category": "Testing"}),
        ("Test Planning", {"aliases": ["test planning", "test plans", "test strategy"], "category": "Testing"}),
        ("Test Case Design", {"aliases": ["test case design", "test cases", "test scenarios"], "category": "Testing"}),
        ("Requirements Analysis", {"aliases": ["requirements analysis", "requirement analysis", "acceptance criteria"], "category": "Testing"}),
        ("Communication", {"aliases": ["communication skills", "communicate", "stakeholder communication"], "category": "Professional"}),
        ("Collaboration", {"aliases": ["collaboration", "cross-functional", "cross functional"], "category": "Professional"}),
    ]
)

REQUIRED_MARKERS = re.compile(
    r"\b(required|must have|must-have|essential|minimum qualifications|you have|need to have|what you bring|qualification)\b",
    re.IGNORECASE,
)
PREFERRED_MARKERS = re.compile(
    r"\b(preferred|nice to have|nice-to-have|bonus|plus|desired|advantage)\b",
    re.IGNORECASE,
)
TECH_TERM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:C\+\+|C#|[A-Z][A-Za-z0-9]+(?:[+.#/_-][A-Za-z0-9]+)*|[a-z]+\d+)(?![A-Za-z0-9])"
)
IGNORED_EXPLICIT_TERMS = {
    "a", "an", "and", "api", "automation", "candidate", "candidates", "company", "degree", "experience",
    "engineer", "engineers", "essential", "job", "minimum", "must", "preferred", "qualification", "qualifications",
    "quality", "qa", "required", "requirements", "responsibilities", "responsibility", "role", "skills", "software",
    "team", "test", "tester", "testing", "the", "we", "with", "you",
}


def clean_text(value: Any) -> str:
    """Strip HTML while preserving line boundaries used for requirement priority."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"</(?:li|p|div|h[1-6])\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]*>", " ", text)
    text = text.replace("\x00", " ").replace("\r", "\n")
    text = re.sub(r"[\t ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def phrase_pattern(phrase: str) -> re.Pattern[str]:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(phrase) + r"(?![A-Za-z0-9])", re.IGNORECASE)


def contains_phrase(text: str, phrase: str) -> bool:
    return bool(phrase_pattern(phrase).search(text))


def skill_hits(text: str) -> list[tuple[int, int, str, str]]:
    """Find one earliest span for each known skill."""
    found: list[tuple[int, int, str, str]] = []
    for canonical, data in QA_SKILLS.items():
        matches = []
        for alias in data["aliases"] + [canonical]:
            match = phrase_pattern(alias).search(text)
            if match:
                matches.append(match)
        if matches:
            first = min(matches, key=lambda match: match.start())
            found.append((first.start(), first.end(), canonical, data["category"]))
    return sorted(found, key=lambda item: item[0])


def extract_skill_mentions(value: Any) -> list[dict[str, str]]:
    """Return every recognised skill once, in the order it first appears."""
    return [
        {"skill": canonical, "category": category}
        for _, _, canonical, category in skill_hits(clean_text(value))
    ]


def requirement_context(text: str, position: int) -> str:
    before = max(text.rfind("\n", 0, position), text.rfind(".", 0, position), text.rfind(";", 0, position))
    after_candidates = [
        index
        for index in (text.find("\n", position), text.find(".", position), text.find(";", position))
        if index >= 0
    ]
    after = min(after_candidates) if after_candidates else len(text)
    return text[max(0, before + 1):after]


def priority_for_position(text: str, position: int) -> str:
    """Classify a requirement from its line and nearest preceding section label."""
    local = requirement_context(text, position)
    if PREFERRED_MARKERS.search(local):
        return "preferred"
    if REQUIRED_MARKERS.search(local):
        return "required"
    prefix = text[:position]
    markers = [(match.start(), "required") for match in REQUIRED_MARKERS.finditer(prefix)]
    markers.extend((match.start(), "preferred") for match in PREFERRED_MARKERS.finditer(prefix))
    if markers:
        marker_position, priority = max(markers, key=lambda item: item[0])
        # A heading such as "Required:" normally governs the following list.
        if position - marker_position <= 900:
            return priority
    return "relevant"


def extract_explicit_other_terms(text: str, known_spans: list[tuple[int, int]]) -> list[tuple[int, str, str, str]]:
    """Keep clearly listed technology names that are not in the compact taxonomy.

    These are visible in the gap map and only count as matched when the exact
    phrase is already supported somewhere in the user's factual profile.
    """
    extras: list[tuple[int, str, str, str]] = []
    seen: set[str] = set()
    for match in TECH_TERM_PATTERN.finditer(text):
        term = match.group(0).strip()
        normalised = term.casefold()
        if normalised in IGNORED_EXPLICIT_TERMS or normalised in seen:
            continue
        if any(match.start() < end and match.end() > start for start, end in known_spans):
            continue
        priority = priority_for_position(text, match.start())
        if priority == "relevant":
            continue
        seen.add(normalised)
        extras.append((match.start(), term, "Additional technology", priority))
    return extras


def extract_requirements(job_text: Any) -> list[dict[str, str]]:
    text = clean_text(job_text)
    known = skill_hits(text)
    requirements = [
        (position, canonical, category, priority_for_position(text, position))
        for position, _, canonical, category in known
    ]
    requirements.extend(extract_explicit_other_terms(text, [(start, end) for start, end, _, _ in known]))
    requirements.sort(key=lambda item: item[0])
    return [
        {"skill": skill, "category": category, "priority": priority}
        for _, skill, category, priority in requirements
    ]


def profile_sources(profile: dict[str, Any]) -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    if profile.get("summary"):
        sources.append(("Summary", str(profile["summary"])))
    if profile.get("headline"):
        sources.append(("Headline", str(profile["headline"])))
    for skill in profile.get("skills") or []:
        sources.append(("Skills", str(skill)))
    for experience in profile.get("experience") or []:
        if not isinstance(experience, dict):
            continue
        label = "Experience"
        if experience.get("title") or experience.get("company"):
            label = "Experience: " + " — ".join(
                item for item in (str(experience.get("title") or ""), str(experience.get("company") or "")) if item
            )
        sources.append((label, str(experience.get("title") or "")))
        for bullet in experience.get("bullets") or []:
            sources.append((label, str(bullet)))
    for project in profile.get("projects") or []:
        if not isinstance(project, dict):
            continue
        label = "Project" + (": " + str(project["name"]) if project.get("name") else "")
        sources.append((label, str(project.get("description") or "")))
        for bullet in project.get("bullets") or []:
            sources.append((label, str(bullet)))
    for certification in profile.get("certifications") or []:
        sources.append(("Certifications", str(certification)))
    return sources


def profile_evidence(profile: dict[str, Any]) -> tuple[set[str], dict[str, list[str]], list[tuple[str, str]]]:
    sources = profile_sources(profile)
    skills: set[str] = set()
    evidence: dict[str, list[str]] = {}
    for label, value in sources:
        for mention in extract_skill_mentions(value):
            skill = mention["skill"]
            skills.add(skill)
            evidence.setdefault(skill, [])
            if label not in evidence[skill]:
                evidence[skill].append(label)
    return skills, evidence, sources


def phrase_evidence(sources: list[tuple[str, str]], phrase: str) -> list[str]:
    labels: list[str] = []
    for label, value in sources:
        if contains_phrase(value, phrase) and label not in labels:
            labels.append(label)
    return labels


def calculate_profile_completion(profile: dict[str, Any]) -> dict[str, Any]:
    is_starter = bool(profile.get("is_starter_template"))
    checks = {
        "Contact details": bool(profile.get("full_name") and profile.get("email")) and not is_starter,
        "Professional headline": bool(profile.get("headline")) and not is_starter,
        "Summary": bool(profile.get("summary")) and not is_starter,
        "Verified skills": bool(profile.get("skills")) and not is_starter,
        "Experience": bool(profile.get("experience")) and not is_starter,
        "Education": bool(profile.get("education")) and not is_starter,
    }
    complete = sum(checks.values())
    return {
        "percent": round(100 * complete / len(checks)),
        "complete": complete,
        "total": len(checks),
        "items": [{"label": label, "complete": is_complete} for label, is_complete in checks.items()],
    }


def readability_score(profile: dict[str, Any]) -> tuple[int, list[str]]:
    score = 0
    advice: list[str] = []
    if profile.get("full_name") and (profile.get("email") or profile.get("phone")):
        score += 20
    else:
        advice.append("Add your name and at least one direct contact method.")
    if profile.get("headline"):
        score += 15
    else:
        advice.append("Use a role-focused professional headline.")
    if profile.get("summary"):
        score += 15
    else:
        advice.append("Add a concise factual professional summary.")
    if profile.get("skills"):
        score += 15
    else:
        advice.append("Add verified technical and testing skills.")
    if profile.get("experience"):
        score += 25
    else:
        advice.append("Add at least one experience entry with evidence-based bullet points.")
    if profile.get("education") or profile.get("certifications"):
        score += 10
    else:
        advice.append("Add education or a relevant certification.")
    return score, advice


def match_profile_to_job(profile: dict[str, Any], job_text: Any, job_title: str = "") -> dict[str, Any]:
    requirements = extract_requirements(job_text)
    known_skills, evidence, sources = profile_evidence(profile)
    requirements_by_skill = {item["skill"]: item for item in requirements}
    evidence_by_skill = dict(evidence)
    matched: list[str] = []
    missing: list[str] = []
    for requirement in requirements:
        skill = requirement["skill"]
        supporting_evidence = evidence.get(skill, []) if skill in known_skills else phrase_evidence(sources, skill)
        if supporting_evidence:
            matched.append(skill)
            evidence_by_skill[skill] = supporting_evidence
        else:
            missing.append(skill)

    required = [item["skill"] for item in requirements if item["priority"] == "required"]
    preferred = [item["skill"] for item in requirements if item["priority"] == "preferred"]
    relevant = [item["skill"] for item in requirements if item["priority"] == "relevant"]
    weighted_total = len(required) * 3 + len(preferred) * 2 + len(relevant)
    weighted_matched = sum(
        3 if requirements_by_skill[skill]["priority"] == "required" else 2 if requirements_by_skill[skill]["priority"] == "preferred" else 1
        for skill in matched
    )
    match_score = round(100 * weighted_matched / weighted_total) if weighted_total else 0
    readability, readability_advice = readability_score(profile)

    # Preserve job-description order so the exported skill section is relevant,
    # while retaining only phrases backed by the factual master profile.
    tailored_skills = list(dict.fromkeys(matched))
    remaining = [skill for skill in known_skills if skill not in tailored_skills]
    tailored_skills.extend(sorted(remaining)[:max(0, 16 - len(tailored_skills))])
    evidence_rows = [{"skill": skill, "evidence": evidence_by_skill.get(skill, ["Verified profile skill"])} for skill in matched]
    guidance = list(readability_advice)
    if missing:
        guidance.insert(0, "Only add a missing keyword if you can support it with genuine experience, training, or a project.")
    if not requirements:
        guidance.insert(0, "No recognisable requirements were detected. Paste the complete job description for a more useful match.")
    return {
        "job_title": clean_text(job_title),
        "job_match_score": match_score,
        "readability_score": readability,
        "requirement_count": len(requirements),
        "requirements": requirements,
        "matched_skills": matched,
        "missing_skills": missing,
        "required_skills": required,
        "preferred_skills": preferred,
        "profile_skills": sorted(known_skills),
        "evidence": evidence_rows,
        "tailored_skills": tailored_skills,
        "guidance": guidance[:6],
        "disclaimer": "Job Match is an explainable preparation signal, not an employer ATS score or a guarantee of an interview.",
    }


def build_tailored_resume(profile: dict[str, Any], analysis: dict[str, Any], target_title: str = "") -> dict[str, Any]:
    """Create an export-safe overlay without mutating the master profile."""
    resume = {key: value for key, value in profile.items()}
    skills = list(analysis.get("tailored_skills") or profile.get("skills") or [])
    for skill in profile.get("skills") or []:
        if skill not in skills and len(skills) < 18:
            skills.append(skill)
    resume["skills"] = skills[:18]
    resume["target_title"] = clean_text(target_title or analysis.get("job_title", ""))
    if not resume.get("summary"):
        facts = resume["skills"][:4]
        focus = f" targeting {resume['target_title']} opportunities" if resume["target_title"] else ""
        resume["summary"] = (
            f"Quality Assurance professional{focus}. Profile highlights verified strengths in "
            + (", ".join(facts) if facts else "software quality and testing")
            + "."
        )
    return resume
