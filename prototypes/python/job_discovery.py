"""Compliant multi-source discovery for QA, QE, Test Engineer and SDET roles.

The module calls documented public job feeds and public employer ATS boards. It
does not scrape LinkedIn or submit applications. Results retain source links and
are ranked locally, with an explicit source-health report for the UI.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from hashlib import sha1
import html
import json
import os
import re
from typing import Any, Callable
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


USER_AGENT = "CareerCraft/1.2 (personal QA job workspace; source-attributed)"
MAX_SOURCE_RESULTS = 80
ROLE_TRACKS = [
    "All QA tracks",
    "Test Engineer",
    "Manual QA",
    "QA Automation",
    "SDET",
    "API Testing",
    "Performance Testing",
    "Mobile Testing",
    "Accessibility Testing",
]


def load_local_env() -> None:
    """Load simple KEY=VALUE settings without overriding process variables."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    try:
        with open(env_path, encoding="utf-8") as env_file:
            lines = env_file.readlines()
    except OSError:
        return
    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        name, value = entry.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and value:
            os.environ.setdefault(name, value)

# These are intentionally labelled as a curated public-company-board watchlist.
# A failed/changed board is reported in the source report rather than silently
# replaced with scraped results. Users can still bring in any employer role via
# direct link, pasted description, CSV, or JSON import.
PRODUCT_COMPANY_BOARDS = [
    {"company": "Canva", "provider": "greenhouse", "token": "canva"},
    {"company": "Cloudflare", "provider": "greenhouse", "token": "cloudflare"},
    {"company": "Coinbase", "provider": "greenhouse", "token": "coinbase"},
    {"company": "Datadog", "provider": "greenhouse", "token": "datadog"},
    {"company": "Figma", "provider": "greenhouse", "token": "figma"},
    {"company": "GitLab", "provider": "greenhouse", "token": "gitlab"},
    {"company": "Notion", "provider": "greenhouse", "token": "notion"},
    {"company": "Postman", "provider": "greenhouse", "token": "postman"},
    {"company": "Stripe", "provider": "greenhouse", "token": "stripe"},
    {"company": "Vercel", "provider": "greenhouse", "token": "vercel"},
]

TITLE_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    ("SDET", re.compile(r"\b(?:sdet|software (?:development|design) engineer in test|software engineer in test)\b", re.I), 62),
    ("QA Automation", re.compile(r"\b(?:qa|quality|test) automation(?: engineer| developer| specialist)?\b|\bautomation test engineer\b", re.I), 56),
    ("API Testing", re.compile(r"\b(?:api|web service) test(?:ing)? engineer\b|\bapi qa\b", re.I), 50),
    ("Performance Testing", re.compile(r"\b(?:performance|load|stress) test(?:ing)? engineer\b", re.I), 50),
    ("Mobile Testing", re.compile(r"\b(?:mobile|ios|android) (?:qa|test(?:ing)?) engineer\b", re.I), 48),
    ("Accessibility Testing", re.compile(r"\b(?:accessibility|a11y) (?:qa|test(?:ing)?)\b", re.I), 48),
    ("Manual QA", re.compile(r"\b(?:manual qa|manual test(?:er|ing)?|functional test(?:er|ing)?)\b", re.I), 46),
    ("Test Engineer", re.compile(r"\b(?:qa|quality)(?: assurance)? (?:engineer|tester|analyst)\b|\btest engineer\b|\bquality engineer\b", re.I), 42),
]
SOFTWARE_CONTEXT = re.compile(
    r"\b(?:software|application|web|mobile|api|backend|frontend|product|saas|automation|selenium|playwright|cypress|python|java|javascript|test case|bug|defect|agile|scrum)\b",
    re.I,
)
TECH_WEIGHTED = {
    "selenium": 8,
    "playwright": 9,
    "cypress": 8,
    "api": 7,
    "postman": 6,
    "rest assured": 8,
    "python": 6,
    "java": 6,
    "javascript": 5,
    "typescript": 5,
    "sql": 5,
    "ci/cd": 6,
    "jenkins": 5,
    "github actions": 5,
    "docker": 4,
    "kubernetes": 4,
    "appium": 7,
    "jmeter": 7,
    "k6": 7,
    "accessibility": 6,
}

INDIA_MARKERS = (
    "india",
    "bengaluru",
    "bangalore",
    "hyderabad",
    "pune",
    "mumbai",
    "delhi",
    "new delhi",
    "gurugram",
    "gurgaon",
    "noida",
    "chennai",
    "kolkata",
    "ahmedabad",
    "kochi",
    "coimbatore",
    "remote - india",
    "remote, india",
)


def compact_text(value: Any, limit: int = 30000) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"</(?:p|li|div|br|h[1-6])\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]*>", " ", text)
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t\r]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:limit]


def public_json(url: str, timeout: int = 13) -> Any:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:  # nosec B310 - fixed provider URLs below
        return json.loads(response.read().decode("utf-8"))


def google_search_credentials() -> tuple[str, str]:
    """Read optional official Google Programmable Search credentials.

    These are deliberately server-side environment variables rather than UI
    fields, so an API key is never exposed to the browser or stored in SQLite.
    """
    load_local_env()
    return (
        os.environ.get("GOOGLE_CUSTOM_SEARCH_API_KEY", "").strip(),
        os.environ.get("GOOGLE_CUSTOM_SEARCH_CX", "").strip(),
    )


def google_search_configured() -> bool:
    key, cx = google_search_credentials()
    return bool(key and cx)


def stable_external_id(source: str, value: Any) -> str:
    text = str(value or "").strip()
    if text:
        return f"{source.casefold().replace(' ', '-')}:{text[:180]}"
    digest = sha1((source + repr(value)).encode("utf-8")).hexdigest()[:18]
    return f"{source.casefold().replace(' ', '-')}:{digest}"


def extract_salary(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = compact_text(value, 260)
        if not text:
            continue
        if re.search(r"(?:[$€£₹]|\b(?:usd|inr|eur|gbp|salary|compensation|package)\b)", text, re.I):
            return text
    return ""


def classify_qa_role(title: Any, description: Any) -> tuple[str, int] | None:
    title_text = compact_text(title, 300)
    body = compact_text(description, 12000)
    joined = f"{title_text}\n{body}"
    if not title_text:
        return None
    for track, pattern, score in TITLE_PATTERNS:
        if pattern.search(title_text):
            # A bare QA/quality title could refer to a non-software domain.
            if track == "Test Engineer" and not SOFTWARE_CONTEXT.search(joined):
                continue
            bonus = sum(weight for term, weight in TECH_WEIGHTED.items() if term in joined.casefold())
            return track, min(99, score + min(30, bonus))
    # Some jobs use a nonstandard title, but avoid classifying a generic
    # Software Engineer role just because its description mentions the QA team.
    # The title must still carry an explicit testing/quality signal.
    if (
        re.search(r"\b(?:qa|quality|test|sdet)\b", title_text, re.I)
        and re.search(r"\b(?:qa|quality assurance|sdet|test automation)\b", joined, re.I)
        and SOFTWARE_CONTEXT.search(joined)
    ):
        bonus = sum(weight for term, weight in TECH_WEIGHTED.items() if term in joined.casefold())
        return "Test Engineer", min(88, 35 + min(35, bonus))
    return None


def normalise_job(
    source: str,
    title: Any,
    company: Any,
    source_url: Any,
    description: Any,
    location: Any = "",
    job_type: Any = "",
    external: Any = "",
    salary: Any = "",
    posted_at: Any = "",
    product_company: bool = False,
    source_note: str = "",
) -> dict[str, Any] | None:
    description_text = compact_text(description)
    title_text = compact_text(title, 180)
    classified = classify_qa_role(title_text, description_text)
    if not classified:
        return None
    role_track, fit_score = classified
    salary_text = extract_salary(salary, description_text)
    if salary_text:
        fit_score = min(99, fit_score + 4)
    if product_company:
        fit_score = min(99, fit_score + 5)
    url = str(source_url or "").strip()
    return {
        "external_id": stable_external_id(source, external or url or f"{company}:{title}"),
        "source": source,
        "source_url": url,
        "title": title_text,
        "company": compact_text(company, 180),
        "location": compact_text(location, 180),
        "job_type": compact_text(job_type, 80),
        "description": description_text or f"Imported {title_text} opportunity from {source}.",
        "salary": salary_text,
        "posted_at": compact_text(posted_at, 80),
        "role_track": role_track,
        "quality_score": fit_score,
        "company_signal": "Product-company public board" if product_company else "",
        "is_product_company": 1 if product_company else 0,
        "source_note": source_note or f"Source-attributed public listing from {source}.",
    }


def matches_market(job: dict[str, Any], market: str = "") -> bool:
    """Keep India discovery focused on listings that explicitly support India."""
    requested = compact_text(market, 120).casefold()
    if not requested or requested in {"all", "worldwide", "anywhere"}:
        return True
    location = compact_text(job.get("location"), 500).casefold()
    if requested in {"india", "india only", "india / remote"}:
        if any(marker in location for marker in INDIA_MARKERS):
            return True
        # A bare global "Remote" role is not an India role. Accept a remote
        # result only when its listing explicitly says candidates may work from
        # India, rather than matching a stray mention of India in the body.
        if "remote" in location:
            evidence = compact_text(job.get("description"), 5000).casefold()
            return bool(
                re.search(r"(?:remote.{0,35}india|india.{0,35}remote|based in india|india[- ]only)", evidence)
            )
        return False
    haystack = " ".join(compact_text(job.get(key), 5000) for key in ("location", "description")).casefold()
    return requested in haystack


def fetch_remotive(query: str) -> list[dict[str, Any]]:
    params = urlencode({"search": query, "limit": 40})
    payload = public_json(f"https://remotive.com/api/remote-jobs?{params}")
    return [
        candidate
        for item in payload.get("jobs", [])
        if (candidate := normalise_job(
            "Remotive", item.get("title"), item.get("company_name"), item.get("url"), item.get("description"),
            item.get("candidate_required_location"), item.get("job_type"), item.get("id"), item.get("salary"),
            item.get("publication_date"), source_note="Remotive public remote-jobs API; listing opens at its attributed source URL.",
        ))
    ]


def fetch_remote_ok(_: str) -> list[dict[str, Any]]:
    payload = public_json("https://remoteok.com/api")
    rows = payload if isinstance(payload, list) else []
    return [
        candidate
        for item in rows
        if isinstance(item, dict) and item.get("position") and (candidate := normalise_job(
            "Remote OK", item.get("position"), item.get("company"), item.get("url") or item.get("apply_url"),
            item.get("description") or item.get("tags"), item.get("location") or "Remote", item.get("employment_type"),
            item.get("id") or item.get("slug"), item.get("salary"), item.get("date"),
            source_note="Remote OK public JSON feed; CareerCraft preserves attribution and the original apply link.",
        ))
    ]


def fetch_jobicy(query: str) -> list[dict[str, Any]]:
    params = urlencode({"count": 50, "tag": query})
    payload = public_json(f"https://jobicy.com/api/v2/remote-jobs?{params}")
    rows = payload.get("jobs", []) if isinstance(payload, dict) else []
    return [
        candidate
        for item in rows
        if isinstance(item, dict) and (candidate := normalise_job(
            "Jobicy", item.get("jobTitle"), item.get("companyName"), item.get("url"), item.get("jobDescription"),
            item.get("jobGeo"), item.get("jobType"), item.get("id") or item.get("url"),
            item.get("annualSalaryMin") or item.get("annualSalaryMax"), item.get("pubDate"),
            source_note="Jobicy public remote-jobs API; links lead to the attributed listing.",
        ))
    ]


def fetch_himalayas(query: str, market: str = "") -> list[dict[str, Any]]:
    params = urlencode({"search": f"{query} {market}".strip(), "limit": 50})
    payload = public_json(f"https://himalayas.app/jobs/api?{params}")
    rows = payload.get("jobs", []) if isinstance(payload, dict) else []
    output = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        restrictions = item.get("locationRestrictions") or []
        location = ", ".join(str(value) for value in restrictions if value) or "Remote"
        salary = ""
        if item.get("minSalary") or item.get("maxSalary"):
            salary = f"{item.get('currency') or ''} {item.get('minSalary') or ''}-{item.get('maxSalary') or ''} {item.get('salaryPeriod') or ''}".strip()
        candidate = normalise_job(
            "Himalayas", item.get("title"), item.get("companyName"), item.get("applicationLink") or item.get("guid"),
            item.get("description") or item.get("excerpt"), location, item.get("employmentType"), item.get("guid"),
            salary, item.get("pubDate"), source_note="Himalayas public jobs API; CareerCraft preserves the original application link.",
        )
        if candidate:
            output.append(candidate)
    return output


def fetch_arbeitnow(_: str) -> list[dict[str, Any]]:
    payload = public_json("https://www.arbeitnow.com/api/job-board-api")
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    return [
        candidate
        for item in rows[:MAX_SOURCE_RESULTS]
        if isinstance(item, dict) and (candidate := normalise_job(
            "Arbeitnow", item.get("title"), item.get("company_name"), item.get("url"), item.get("description"),
            item.get("location"), item.get("job_types"), item.get("slug") or item.get("url"), "", item.get("created_at"),
            source_note="Arbeitnow public job-board API; source and original URL are retained.",
        ))
    ]


def fetch_the_muse(_: str) -> list[dict[str, Any]]:
    payload = public_json("https://www.themuse.com/api/public/jobs?page=1&descending=true&category=Engineering")
    rows = payload.get("results", []) if isinstance(payload, dict) else []
    output = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        company = item.get("company") or {}
        locations = item.get("locations") or []
        categories = item.get("categories") or []
        candidate = normalise_job(
            "The Muse", item.get("name"), company.get("name") if isinstance(company, dict) else company,
            item.get("refs", {}).get("landing_page") if isinstance(item.get("refs"), dict) else "", item.get("contents"),
            ", ".join(str(location.get("name", "")) for location in locations if isinstance(location, dict)),
            ", ".join(str(category.get("name", "")) for category in categories if isinstance(category, dict)),
            item.get("id"), "", item.get("publication_date"),
            source_note="The Muse public jobs API; CareerCraft shows the attributed listing link.",
        )
        if candidate:
            output.append(candidate)
    return output


def fetch_google_web_jobs(query: str, market: str = "India") -> list[dict[str, Any]]:
    """Use Google's documented Programmable Search API when the owner configures it.

    This is web-search output pointing to employer boards, not scraped Google
    Jobs data. It is optional because Google requires a Search Engine ID and an
    API key for the official API.
    """
    key, cx = google_search_credentials()
    if not key or not cx:
        return []
    search = f'({query}) jobs {market} (site:greenhouse.io OR site:jobs.lever.co OR site:jobs.ashbyhq.com OR site:workdayjobs.com)'
    params = urlencode({"key": key, "cx": cx, "q": search, "num": 10, "gl": "in", "cr": "countryIN"})
    payload = public_json(f"https://www.googleapis.com/customsearch/v1?{params}")
    output = []
    for item in payload.get("items", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("link") or "")
        parsed = urlparse(url)
        candidate = normalise_job(
            "Google web job search",
            item.get("title"),
            parsed.netloc.removeprefix("www."),
            url,
            item.get("snippet"),
            market,
            "",
            item.get("cacheId") or url,
            "",
            "",
            source_note="Official Google Programmable Search result; open the employer board and paste the full description before tailoring.",
        )
        if candidate:
            output.append(candidate)
    return output


def fetch_greenhouse_board(company: str, token: str) -> list[dict[str, Any]]:
    safe_token = re.sub(r"[^A-Za-z0-9_-]", "", token)
    if not safe_token:
        return []
    payload = public_json(f"https://boards-api.greenhouse.io/v1/boards/{safe_token}/jobs?content=true")
    output = []
    for item in payload.get("jobs", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        candidate = normalise_job(
            "Product company board", item.get("title"), company, item.get("absolute_url"), item.get("content"),
            (item.get("location") or {}).get("name", "") if isinstance(item.get("location"), dict) else "",
            "", item.get("id"), "", item.get("updated_at"), product_company=True,
            source_note=f"Public Greenhouse board for {company}; apply only through the employer's original URL.",
        )
        if candidate:
            output.append(candidate)
    return output


def discover_qa_jobs(
    query: str = "qa test engineer",
    include_product_boards: bool = True,
    enabled_sources: set[str] | None = None,
    market: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Aggregate available public QA job sources concurrently.

    Each source is independently fault tolerant: a provider failure appears in
    `source_report`, while results from the other providers remain usable.
    """
    enabled = {item.casefold() for item in enabled_sources} if enabled_sources else set()
    source_tasks: list[tuple[str, Callable[[], list[dict[str, Any]]]]] = []
    providers = [
        ("Remotive", lambda: fetch_remotive(query)),
        ("Remote OK", lambda: fetch_remote_ok(query)),
        ("Jobicy", lambda: fetch_jobicy(query)),
        ("Himalayas", lambda: fetch_himalayas(query, market)),
        ("Arbeitnow", lambda: fetch_arbeitnow(query)),
        ("The Muse", lambda: fetch_the_muse(query)),
    ]
    google_enabled = not enabled or "google web job search" in enabled
    if google_enabled and google_search_configured():
        providers.append(("Google web job search", lambda: fetch_google_web_jobs(query, market or "India")))
    for name, call in providers:
        if not enabled or name.casefold() in enabled:
            source_tasks.append((name, call))
    if include_product_boards and (not enabled or "product company boards" in enabled):
        for board in PRODUCT_COMPANY_BOARDS:
            source_tasks.append((f"{board['company']} careers", lambda board=board: fetch_greenhouse_board(board["company"], board["token"])))

    all_jobs: list[dict[str, Any]] = []
    report: list[dict[str, Any]] = []
    if google_enabled and not google_search_configured():
        report.append(
            {
                "source": "Google web job search",
                "status": "not_configured",
                "count": 0,
                "detail": "Add GOOGLE_CUSTOM_SEARCH_API_KEY and GOOGLE_CUSTOM_SEARCH_CX to enable official Google web results.",
            }
        )
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(source_tasks)))) as pool:
        futures = {pool.submit(call): name for name, call in source_tasks}
        for future in as_completed(futures):
            name = futures[future]
            try:
                rows = future.result()
                all_jobs.extend(rows)
                report.append({"source": name, "status": "ok", "count": len(rows)})
            except Exception as exc:
                report.append({"source": name, "status": "unavailable", "count": 0, "detail": str(exc)[:180]})

    unique: dict[str, dict[str, Any]] = {}
    for job in all_jobs:
        key = job["source_url"] or job["external_id"]
        current = unique.get(key)
        if not current or int(job.get("quality_score", 0)) > int(current.get("quality_score", 0)):
            unique[key] = job
    market_jobs = [job for job in unique.values() if matches_market(job, market)]
    if market:
        for item in report:
            if item.get("status") != "ok":
                continue
            source_name = str(item.get("source") or "")
            source_jobs = [
                job
                for job in unique.values()
                if job.get("source") == source_name
                or (
                    source_name.endswith(" careers")
                    and job.get("source") == "Product company board"
                    and job.get("company") == source_name.removesuffix(" careers")
                )
            ]
            matched_count = sum(1 for job in source_jobs if matches_market(job, market))
            raw_count = len(source_jobs)
            item["count"] = matched_count
            if raw_count != matched_count:
                item["detail"] = f"{raw_count} QA roles found; {matched_count} match {market} scope."
    ranked = sorted(
        market_jobs,
        key=lambda item: (
            int(item.get("is_product_company") or 0),
            bool(item.get("salary")),
            int(item.get("quality_score") or 0),
            item.get("posted_at") or "",
        ),
        reverse=True,
    )[:180]
    report.sort(key=lambda item: item["source"])
    return ranked, report


def source_catalogue() -> list[dict[str, str]]:
    return [
        {"name": "Remotive", "type": "Public remote job API"},
        {"name": "Remote OK", "type": "Public JSON job feed"},
        {"name": "Jobicy", "type": "Public remote-job API"},
        {"name": "Himalayas", "type": "Public remote-jobs API; no key required"},
        {"name": "Arbeitnow", "type": "Public job-board API"},
        {"name": "The Muse", "type": "Public jobs API"},
        {"name": "Product company boards", "type": "Curated public Greenhouse career boards"},
        {"name": "Google web job search", "type": "Optional official Programmable Search API; requires server-side key and Search Engine ID"},
    ]
