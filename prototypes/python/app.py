"""CareerCraft: a local-first, ATS-aware resume workspace.

The application deliberately keeps a factual master profile separate from a
job-specific resume version.  Tailoring can prioritise evidence already in the
profile, but it never adds an unverified skill or credential.
"""

from __future__ import annotations

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO, StringIO
import html
import json
import os
from pathlib import Path
import re
import sqlite3
from threading import Lock
from uuid import uuid4
from typing import Any
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from ats_engine import (
    build_tailored_resume,
    calculate_profile_completion,
    match_profile_to_job,
)
from docx_builder import available_resume_layouts, build_resume_document, resume_filename
from job_discovery import ROLE_TRACKS, discover_qa_jobs, source_catalogue
from local_ai import DEFAULT_MODEL, local_ai_review, ollama_status, pull_local_model, start_ollama_service, valid_model_name
from profile_importer import import_resume_text as build_import_draft
from resume_parser import extract_text_from_bytes
from starter_profile import has_starter_placeholders, qa_starter_profile
from workspace_assistant import apply_proposal as apply_workspace_proposal
from workspace_assistant import chat as workspace_chat


HERE = Path(__file__).resolve().parent
DEFAULT_DB_PATH = HERE / "data.db"
ALLOWED_UPLOADS = {".txt", ".pdf", ".docx"}
JOB_STATUSES = {"new", "approved", "rejected", "applied", "interview", "offer", "archived", "closed"}
APPLICATION_STATUSES = {"approved", "applied", "interview", "offer"}
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
RESUME_LAYOUT_IDS = {item["id"] for item in available_resume_layouts()}
CHAT_TASKS: dict[str, dict[str, Any]] = {}
CHAT_TASKS_LOCK = Lock()
CHAT_TASK_EXECUTOR = ThreadPoolExecutor(max_workers=2)


class ApiInputError(ValueError):
    """A client sent an invalid JSON request to an API endpoint."""

DEFAULT_PROFILE: dict[str, Any] = {
    "full_name": "",
    "headline": "",
    "email": "",
    "phone": "",
    "location": "",
    "linkedin_url": "",
    "portfolio_url": "",
    "summary": "",
    "skills": [],
    "experience": [],
    "education": [],
    "certifications": [],
    "projects": [],
    "is_starter_template": False,
    "template_name": "",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def update_chat_task(task_id: str, **values: Any) -> None:
    with CHAT_TASKS_LOCK:
        if task_id in CHAT_TASKS:
            CHAT_TASKS[task_id].update(values)


def run_chat_task(task_id: str, message: str, selected_model: str, profile: dict[str, Any]) -> None:
    update_chat_task(task_id, state="running", stage="thinking", detail="Understanding your request locally.")
    try:
        if any(term in message.casefold() for term in ("google", "search", "job", "jobs", "url", "filter")):
            update_chat_task(task_id, stage="searching", detail="Checking CareerCraft's configured public job sources.")
        result = workspace_chat(message, selected_model, profile)
        update_chat_task(task_id, state="complete", stage="complete", detail="Response ready.", result=result)
    except Exception as exc:
        update_chat_task(task_id, state="error", stage="error", detail=str(exc))


def json_value(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def request_json_object() -> dict[str, Any]:
    """Return a JSON object, never quietly treating malformed input as empty."""
    if not request.is_json:
        raise ApiInputError("Send a JSON object in the request body.")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiInputError("The request body must be a valid JSON object.")
    return data


def plain_text(value: Any, limit: int = 20000) -> str:
    """Clean stored/imported text without attempting to fetch a supplied URL."""
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]*>", " ", text)
    text = text.replace("\x00", " ")
    text = re.sub(r"[\t\r ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:limit]


def clean_url(value: Any) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return value[:2000]


def list_of_strings(value: Any, maximum: int = 60) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,\n]", value)
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        item = plain_text(item, 160)
        if item and item not in output:
            output.append(item)
        if len(output) >= maximum:
            break
    return output


def normalise_profile(payload: Any) -> dict[str, Any]:
    """Accept only the factual fields used in the resume model."""
    source = payload if isinstance(payload, dict) else {}
    profile = dict(DEFAULT_PROFILE)
    for key in (
        "full_name",
        "headline",
        "email",
        "phone",
        "location",
        "linkedin_url",
        "portfolio_url",
        "summary",
    ):
        profile[key] = plain_text(source.get(key, ""), 3000 if key == "summary" else 250)

    profile["linkedin_url"] = clean_url(profile["linkedin_url"])
    profile["portfolio_url"] = clean_url(profile["portfolio_url"])
    profile["skills"] = list_of_strings(source.get("skills"), 80)
    profile["certifications"] = list_of_strings(source.get("certifications"), 40)

    raw_experience = source.get("experience")
    experiences: list[dict[str, Any]] = []
    for entry in (raw_experience if isinstance(raw_experience, list) else [])[:15]:
        if not isinstance(entry, dict):
            continue
        item = {
            "title": plain_text(entry.get("title"), 180),
            "company": plain_text(entry.get("company"), 180),
            "location": plain_text(entry.get("location"), 180),
            "start_date": plain_text(entry.get("start_date"), 50),
            "end_date": plain_text(entry.get("end_date"), 50),
            "current": bool(entry.get("current")),
            "bullets": list_of_strings(entry.get("bullets"), 12),
        }
        if any(item[k] for k in ("title", "company", "bullets")):
            experiences.append(item)
    profile["experience"] = experiences

    raw_education = source.get("education")
    education: list[dict[str, Any]] = []
    for entry in (raw_education if isinstance(raw_education, list) else [])[:10]:
        if not isinstance(entry, dict):
            continue
        item = {
            "degree": plain_text(entry.get("degree"), 220),
            "school": plain_text(entry.get("school"), 220),
            "location": plain_text(entry.get("location"), 180),
            "graduation": plain_text(entry.get("graduation"), 80),
        }
        if item["degree"] or item["school"]:
            education.append(item)
    profile["education"] = education

    raw_projects = source.get("projects")
    projects: list[dict[str, Any]] = []
    for entry in (raw_projects if isinstance(raw_projects, list) else [])[:10]:
        if not isinstance(entry, dict):
            continue
        item = {
            "name": plain_text(entry.get("name"), 220),
            "url": clean_url(entry.get("url")),
            "description": plain_text(entry.get("description"), 900),
            "bullets": list_of_strings(entry.get("bullets"), 8),
        }
        if item["name"] or item["description"] or item["bullets"]:
            projects.append(item)
    profile["projects"] = projects
    profile["is_starter_template"] = bool(source.get("is_starter_template", False))
    profile["template_name"] = plain_text(source.get("template_name"), 100)
    return profile


def database_path(app: Flask) -> str:
    return app.config["DATABASE"]


def get_db(app: Flask) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path(app))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(app: Flask) -> None:
    with closing(get_db(app)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS resumes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                email TEXT,
                filename TEXT,
                content TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS profile (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT UNIQUE,
                source TEXT NOT NULL DEFAULT 'Manual',
                source_url TEXT,
                title TEXT NOT NULL,
                company TEXT,
                location TEXT,
                job_type TEXT,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'approved',
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS job_searches (
                cache_key TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                market TEXT NOT NULL,
                role_track TEXT NOT NULL,
                product_only INTEGER NOT NULL DEFAULT 0,
                salary_only INTEGER NOT NULL DEFAULT 0,
                source_report TEXT NOT NULL DEFAULT '[]',
                checked_at TEXT NOT NULL,
                result_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS resume_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                title TEXT NOT NULL,
                filename TEXT NOT NULL,
                profile_snapshot TEXT NOT NULL,
                tailored_snapshot TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_versions_created ON resume_versions(created_at DESC);
            """
        )
        # SQLite has no ADD COLUMN IF NOT EXISTS. Keep existing local workspaces
        # compatible while adding transparent discovery metadata.
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        job_columns = {
            "salary": "TEXT NOT NULL DEFAULT ''",
            "posted_at": "TEXT NOT NULL DEFAULT ''",
            "role_track": "TEXT NOT NULL DEFAULT ''",
            "quality_score": "INTEGER NOT NULL DEFAULT 0",
            "company_signal": "TEXT NOT NULL DEFAULT ''",
            "is_product_company": "INTEGER NOT NULL DEFAULT 0",
            "source_note": "TEXT NOT NULL DEFAULT ''",
            "closed_at": "TEXT",
        }
        for column, definition in job_columns.items():
            if column not in existing_columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")
        application_columns = {
            "application_kind": "TEXT NOT NULL DEFAULT 'Online application'",
            "contact_name": "TEXT NOT NULL DEFAULT ''",
            "contact_role": "TEXT NOT NULL DEFAULT ''",
            "contact_email": "TEXT NOT NULL DEFAULT ''",
            "contact_phone": "TEXT NOT NULL DEFAULT ''",
            "referral_name": "TEXT NOT NULL DEFAULT ''",
            "referral_contact": "TEXT NOT NULL DEFAULT ''",
            "next_step": "TEXT NOT NULL DEFAULT ''",
        }
        existing_application_columns = {row["name"] for row in conn.execute("PRAGMA table_info(applications)").fetchall()}
        for column, definition in application_columns.items():
            if column not in existing_application_columns:
                conn.execute(f"ALTER TABLE applications ADD COLUMN {column} {definition}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_track ON jobs(role_track)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_job_searches_updated ON job_searches(updated_at DESC)")
        conn.commit()


def read_profile(app: Flask) -> dict[str, Any]:
    with closing(get_db(app)) as conn:
        row = conn.execute("SELECT data FROM profile WHERE id = 1").fetchone()
    if not row:
        return normalise_profile(qa_starter_profile())
    return normalise_profile(json_value(row["data"], DEFAULT_PROFILE))


def write_profile(app: Flask, payload: Any) -> dict[str, Any]:
    profile = normalise_profile(payload)
    # The template marker is derived from visible placeholders, not trusted from
    # the browser. A fully replaced starter becomes a normal factual profile.
    profile["is_starter_template"] = has_starter_placeholders(profile)
    if not profile["is_starter_template"]:
        profile["template_name"] = ""
    now = utc_now()
    with closing(get_db(app)) as conn:
        conn.execute(
            """
            INSERT INTO profile (id, data, updated_at) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at
            """,
            (json.dumps(profile, ensure_ascii=False), now),
        )
        conn.commit()
    return profile


def get_setting(app: Flask, key: str) -> str | None:
    with closing(get_db(app)) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(app: Flask, key: str, value: str) -> None:
    now = utc_now()
    with closing(get_db(app)) as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now),
        )
        conn.commit()


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def job_search_cache_key(
    query: str,
    market: str,
    role_track: str,
    product_only: bool,
    salary_only: bool,
    sources: set[str],
) -> str:
    payload = json.dumps(
        {
            "query": query.casefold(),
            "market": market.casefold(),
            "role_track": role_track.casefold(),
            "product_only": product_only,
            "salary_only": salary_only,
            "sources": sorted(source.casefold() for source in sources),
        },
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def read_job_search_cache(app: Flask, cache_key: str) -> dict[str, Any] | None:
    with closing(get_db(app)) as conn:
        row = conn.execute("SELECT * FROM job_searches WHERE cache_key = ?", (cache_key,)).fetchone()
    if not row:
        return None
    result = {key: row[key] for key in row.keys()}
    result["source_report"] = json_value(result.get("source_report"), [])
    return result


def write_job_search_cache(
    app: Flask,
    cache_key: str,
    query: str,
    market: str,
    role_track: str,
    product_only: bool,
    salary_only: bool,
    source_report: list[dict[str, Any]],
    result_count: int,
    last_error: str = "",
) -> None:
    now = utc_now()
    with closing(get_db(app)) as conn:
        conn.execute(
            """
            INSERT INTO job_searches
            (cache_key, query, market, role_track, product_only, salary_only, source_report, checked_at, result_count, last_error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                source_report = excluded.source_report,
                checked_at = excluded.checked_at,
                result_count = excluded.result_count,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            (
                cache_key,
                query,
                market,
                role_track,
                int(product_only),
                int(salary_only),
                json.dumps(source_report, ensure_ascii=False),
                now,
                result_count,
                plain_text(last_error, 500),
                now,
            ),
        )
        conn.commit()


def job_to_dict(row: sqlite3.Row, profile: dict[str, Any]) -> dict[str, Any]:
    result = {key: row[key] for key in row.keys()}
    result["analysis"] = match_profile_to_job(profile, row["description"], row["title"])
    result["qa_fit_score"] = int(result.get("quality_score") or 0)
    result["role_track"] = result.get("role_track") or "Test Engineer"
    result["is_product_company"] = bool(result.get("is_product_company"))
    return result


def query_job(app: Flask, job_id: int) -> sqlite3.Row:
    with closing(get_db(app)) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        abort(404, description="Job not found")
    return row


def create_job(app: Flask, payload: dict[str, Any], source: str = "Manual") -> tuple[dict[str, Any], bool]:
    title = plain_text(payload.get("title"), 180) or "QA opportunity"
    description = plain_text(payload.get("description"), 30000)
    if len(description) < 30:
        raise ValueError("Paste a fuller job description (at least 30 characters).")
    external_id = plain_text(payload.get("external_id"), 250) or None
    try:
        quality_score = min(99, max(0, int(payload.get("quality_score") or 0)))
    except (TypeError, ValueError):
        quality_score = 0
    now = utc_now()
    values = (
        external_id,
        plain_text(payload.get("source") or source, 80),
        clean_url(payload.get("source_url")),
        title,
        plain_text(payload.get("company"), 180),
        plain_text(payload.get("location"), 180),
        plain_text(payload.get("job_type"), 80),
        description,
        plain_text(payload.get("salary"), 220),
        plain_text(payload.get("posted_at"), 80),
        plain_text(payload.get("role_track"), 80),
        quality_score,
        plain_text(payload.get("company_signal"), 220),
        int(bool(payload.get("is_product_company"))),
        plain_text(payload.get("source_note"), 500),
        now,
        now,
    )
    with closing(get_db(app)) as conn:
        try:
            cursor = conn.execute(
                """
                INSERT INTO jobs
                (external_id, source, source_url, title, company, location, job_type, description,
                 salary, posted_at, role_track, quality_score, company_signal, is_product_company, source_note,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            job_id = cursor.lastrowid
            conn.commit()
            created = True
        except sqlite3.IntegrityError:
            if not external_id:
                raise
            row = conn.execute("SELECT * FROM jobs WHERE external_id = ?", (external_id,)).fetchone()
            if not row:
                raise
            job_id = row["id"]
            created = False
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return job_to_dict(row, read_profile(app)), created


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.from_mapping(
        # A local run gets an ephemeral secret instead of a published default.
        # Set RESUME_SECRET_KEY explicitly whenever a deployment needs stable
        # signed sessions or runs more than one process.
        SECRET_KEY=os.environ.get("RESUME_SECRET_KEY") or os.urandom(32),
        DATABASE=os.environ.get("RESUME_DB_PATH", str(DEFAULT_DB_PATH)),
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
    )
    if test_config:
        app.config.update(test_config)
    init_db(app)

    @app.context_processor
    def inject_navigation() -> dict[str, Any]:
        return {"app_name": "CareerCraft"}

    @app.get("/")
    def dashboard() -> str:
        return render_template("dashboard.html", page="dashboard")

    @app.get("/profile")
    def profile_page() -> str:
        return render_template("profile.html", page="profile")

    @app.get("/builder")
    def builder_page() -> str:
        return render_template("builder.html", page="builder")

    @app.get("/jobs")
    def jobs_page() -> str:
        return render_template("jobs.html", page="jobs")

    @app.get("/applications")
    def applications_page() -> str:
        return render_template("applications.html", page="applications")

    @app.get("/resources")
    def resources_page() -> str:
        return render_template("resources.html", page="resources")

    @app.get("/assistant")
    def assistant_page() -> str:
        return render_template("assistant.html", page="assistant")

    @app.get("/resumes")
    def legacy_resumes() -> Any:
        return redirect("/builder")

    @app.get("/api/health")
    def health() -> Any:
        return jsonify({"status": "ok", "service": "careercraft"})

    @app.get("/api/profile")
    def profile_api() -> Any:
        profile = read_profile(app)
        return jsonify({"profile": profile, "completion": calculate_profile_completion(profile)})

    @app.put("/api/profile")
    def update_profile() -> Any:
        profile = write_profile(app, request_json_object())
        return jsonify(
            {
                "profile": profile,
                "completion": calculate_profile_completion(profile),
                "message": "Your factual master profile was saved locally.",
            }
        )

    @app.get("/api/profile/starter")
    def starter_profile_api() -> Any:
        profile = normalise_profile(qa_starter_profile())
        return jsonify(
            {
                "profile": profile,
                "completion": calculate_profile_completion(profile),
                "message": "Loaded an editable QA/Test Engineer starter. Replace every visible placeholder with your own facts.",
            }
        )

    @app.get("/api/profile/export")
    def export_profile() -> Any:
        payload = {
            "schema_version": 1,
            "exported_at": utc_now(),
            "profile": read_profile(app),
        }
        buffer = BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        return send_file(buffer, mimetype="application/json", as_attachment=True, download_name="careercraft-profile.json")

    @app.post("/api/profile/import")
    def import_profile_json() -> Any:
        uploaded = request.files.get("file")
        if not uploaded or not uploaded.filename:
            return jsonify({"error": "Choose a CareerCraft profile JSON file."}), 400
        filename = secure_filename(uploaded.filename)
        if Path(filename).suffix.lower() != ".json":
            return jsonify({"error": "Profile import accepts a .json file."}), 400
        raw = uploaded.read(1_500_001)
        if len(raw) > 1_500_000:
            return jsonify({"error": "The profile JSON exceeds the 1.5 MB limit."}), 413
        try:
            parsed = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return jsonify({"error": "This is not a readable profile JSON file."}), 422
        candidate = parsed.get("profile") if isinstance(parsed, dict) and isinstance(parsed.get("profile"), dict) else parsed
        if not isinstance(candidate, dict):
            return jsonify({"error": "The JSON must contain a profile object."}), 422
        profile = normalise_profile(candidate)
        return jsonify(
            {
                "profile": profile,
                "completion": calculate_profile_completion(profile),
                "message": "Review the imported draft, then save it to make it your master profile.",
            }
        )

    @app.post("/api/profile/import-document")
    def import_profile_document() -> Any:
        uploaded = request.files.get("file")
        if not uploaded or not uploaded.filename:
            return jsonify({"error": "Choose a .docx, .pdf, or .txt resume file."}), 400
        filename = secure_filename(uploaded.filename)
        extension = Path(filename).suffix.lower()
        if extension not in ALLOWED_UPLOADS:
            return jsonify({"error": "Only .docx, .pdf, and .txt files are supported."}), 400
        raw = uploaded.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            return jsonify({"error": "The file exceeds the 8 MB limit."}), 413
        try:
            text = extract_text_from_bytes(filename, raw)
        except Exception:
            return jsonify({"error": "This document could not be read. Try a text-based PDF or DOCX."}), 422
        draft = build_import_draft(plain_text(text, 30000))
        draft["profile"] = normalise_profile(draft["profile"])
        draft["completion"] = calculate_profile_completion(draft["profile"])
        draft["filename"] = filename
        return jsonify(draft)

    @app.post("/api/analyze")
    def analyze_job() -> Any:
        data = request_json_object()
        description = plain_text(data.get("description") or data.get("posting_text"), 30000)
        if len(description) < 30:
            return jsonify({"error": "Paste the job description so its requirements can be analysed."}), 400
        title = plain_text(data.get("title"), 180)
        profile = read_profile(app)
        analysis = match_profile_to_job(profile, description, title)
        tailored = build_tailored_resume(profile, analysis, title)
        return jsonify({"analysis": analysis, "tailored_resume": tailored})

    @app.get("/api/ai/status")
    def ai_status() -> Any:
        selected_model = get_setting(app, "local_ai_model") or DEFAULT_MODEL
        return jsonify(ollama_status(selected_model))

    @app.put("/api/ai/settings")
    def update_ai_settings() -> Any:
        data = request_json_object()
        model = plain_text(data.get("model"), 100) or DEFAULT_MODEL
        if not valid_model_name(model):
            return jsonify({"error": "Use a valid local Ollama model name."}), 400
        set_setting(app, "local_ai_model", model)
        return jsonify(ollama_status(model))

    @app.post("/api/ai/start")
    def start_local_ai() -> Any:
        data = request_json_object()
        model = plain_text(data.get("model"), 100) or get_setting(app, "local_ai_model") or DEFAULT_MODEL
        try:
            return jsonify(start_ollama_service(model))
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

    @app.post("/api/ai/pull")
    def pull_ai_model() -> Any:
        data = request_json_object()
        model = plain_text(data.get("model"), 100) or get_setting(app, "local_ai_model") or DEFAULT_MODEL
        try:
            return jsonify(pull_local_model(model))
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

    @app.post("/api/ai/review")
    def review_with_local_ai() -> Any:
        data = request_json_object()
        task = plain_text(data.get("task"), 40).casefold()
        if task not in {"proofread", "resume_review"}:
            return jsonify({"error": "Choose proofread or resume_review."}), 400
        if task == "proofread" and not plain_text(data.get("text"), 14000):
            return jsonify({"error": "Add text before running a proofreading review."}), 400
        selected_model = get_setting(app, "local_ai_model") or DEFAULT_MODEL
        result = local_ai_review(task, data, selected_model)
        result["privacy_note"] = "Resume content is sent only to localhost when Ollama is installed; otherwise the built-in review runs locally in this app."
        return jsonify(result)

    @app.post("/api/workspace-chat/start")
    def start_workspace_chat() -> Any:
        data = request_json_object()
        message = plain_text(data.get("message"), 5000)
        if not message:
            return jsonify({"error": "Write a request for the local workspace assistant."}), 400
        selected_model = get_setting(app, "local_ai_model") or DEFAULT_MODEL
        task_id = uuid4().hex
        with CHAT_TASKS_LOCK:
            CHAT_TASKS[task_id] = {"state": "queued", "stage": "queued", "detail": "Request queued locally."}
        CHAT_TASK_EXECUTOR.submit(run_chat_task, task_id, message, selected_model, read_profile(app))
        return jsonify({"task_id": task_id}), 202

    @app.get("/api/workspace-chat/status/<task_id>")
    def workspace_chat_status(task_id: str) -> Any:
        with CHAT_TASKS_LOCK:
            task = dict(CHAT_TASKS.get(task_id) or {})
        if not task:
            return jsonify({"error": "Assistant request not found or expired."}), 404
        return jsonify(task)

    @app.post("/api/workspace-chat")
    def workspace_chat_api() -> Any:
        """Compatibility endpoint for clients that still expect one response."""
        data = request_json_object()
        selected_model = get_setting(app, "local_ai_model") or DEFAULT_MODEL
        try:
            return jsonify(workspace_chat(plain_text(data.get("message"), 5000), selected_model, read_profile(app)))
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

    @app.post("/api/workspace-chat/apply")
    def apply_workspace_chat_proposal() -> Any:
        data = request_json_object()
        if data.get("confirm") is not True:
            return jsonify({"error": "Review the proposal and explicitly confirm before applying it."}), 400
        try:
            return jsonify(apply_workspace_proposal(plain_text(data.get("proposal_id"), 200)))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409

    # Compatibility endpoint for the original prototype's matching form.
    @app.post("/score")
    def score() -> Any:
        data = request_json_object()
        description = plain_text(data.get("posting_text"), 30000)
        resume_text = plain_text(data.get("resume_text"), 30000)
        temporary_profile = normalise_profile({"summary": resume_text, "skills": []})
        analysis = match_profile_to_job(temporary_profile, description, "")
        return jsonify(
            {
                "posting_skill_count": analysis["requirement_count"],
                "resume_skill_count": len(analysis["profile_skills"]),
                "matched_count": len(analysis["matched_skills"]),
                "score_percent": analysis["job_match_score"],
                "matched": analysis["matched_skills"],
                "suggestions": analysis["missing_skills"],
            }
        )

    @app.post("/api/jobs")
    def add_job() -> Any:
        try:
            job, created = create_job(app, request_json_object())
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"job": job, "created": created}), 201 if created else 200

    @app.patch("/api/jobs/<int:job_id>")
    def update_job(job_id: int) -> Any:
        data = request_json_object()
        existing = query_job(app, job_id)
        description = plain_text(data.get("description"), 30000)
        if len(description) < 30:
            return jsonify({"error": "Paste a fuller job description (at least 30 characters)."}), 400
        now = utc_now()
        with closing(get_db(app)) as conn:
            conn.execute(
                """
                UPDATE jobs
                SET source_url = ?, title = ?, company = ?, location = ?, job_type = ?, description = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    clean_url(data.get("source_url")) or existing["source_url"],
                    plain_text(data.get("title"), 180) or existing["title"],
                    plain_text(data.get("company"), 180) or existing["company"],
                    plain_text(data.get("location"), 180) or existing["location"],
                    plain_text(data.get("job_type"), 80) or existing["job_type"],
                    description,
                    now,
                    job_id,
                ),
            )
            conn.commit()
            updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return jsonify({"job": job_to_dict(updated, read_profile(app)), "updated": True})

    @app.get("/api/jobs")
    def list_jobs() -> Any:
        status = request.args.get("status", "all")
        if status != "all" and status not in JOB_STATUSES:
            return jsonify({"error": "Unsupported job status."}), 400
        try:
            limit = min(max(int(request.args.get("limit", "100")), 1), 100)
        except ValueError:
            return jsonify({"error": "limit must be a whole number."}), 400
        role_track = plain_text(request.args.get("role_track"), 80)
        if role_track and role_track not in ROLE_TRACKS:
            return jsonify({"error": "Unsupported QA role track."}), 400
        product_only = request.args.get("product_only", "").casefold() in {"1", "true", "yes"}
        salary_only = request.args.get("salary_only", "").casefold() in {"1", "true", "yes"}
        clauses: list[str] = []
        values: list[Any] = []
        if status == "all":
            # Closed cards are intentionally out of the everyday queue; users
            # can choose the dedicated Closed filter when they need to restore one.
            clauses.append("status != 'closed'")
        else:
            clauses.append("status = ?")
            values.append(status)
        if role_track and role_track != "All QA tracks":
            clauses.append("role_track = ?")
            values.append(role_track)
        if product_only:
            clauses.append("is_product_company = 1")
        if salary_only:
            clauses.append("TRIM(salary) != ''")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with closing(get_db(app)) as conn:
            rows = conn.execute(
                "SELECT * FROM jobs" + where + " ORDER BY is_product_company DESC, CASE WHEN TRIM(salary) != '' THEN 1 ELSE 0 END DESC, quality_score DESC, updated_at DESC, id DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        profile = read_profile(app)
        return jsonify({"jobs": [job_to_dict(row, profile) for row in rows]})

    @app.get("/api/job-sources")
    def job_sources_api() -> Any:
        return jsonify(
            {
                "role_tracks": ROLE_TRACKS,
                "sources": source_catalogue(),
                "notice": "Results are source-attributed public listings. Company reputation and pay are not guaranteed; review the original employer page and your own compensation research before applying.",
            }
        )

    @app.get("/api/jobs/<int:job_id>")
    def get_job(job_id: int) -> Any:
        return jsonify({"job": job_to_dict(query_job(app, job_id), read_profile(app))})

    @app.post("/api/jobs/<int:job_id>/decision")
    def decide_job(job_id: int) -> Any:
        data = request_json_object()
        status = str(data.get("status", "")).lower()
        if status not in {"approved", "rejected", "applied", "interview", "offer", "closed"}:
            return jsonify({"error": "Choose approved, rejected, applied, interview, offer, or closed."}), 400
        now = utc_now()
        with closing(get_db(app)) as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not job:
                return jsonify({"error": "Job not found."}), 404
            conn.execute(
                "UPDATE jobs SET status = ?, closed_at = ?, updated_at = ? WHERE id = ?",
                (status, now if status == "closed" else None, now, job_id),
            )
            if status in {"rejected", "closed"}:
                # A removed opportunity should not remain in the active
                # application pipeline.
                conn.execute("DELETE FROM applications WHERE job_id = ?", (job_id,))
            else:
                conn.execute(
                    """
                    INSERT INTO applications (job_id, status, notes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at
                    """,
                    (job_id, status, plain_text(data.get("notes"), 2000), now, now),
                )
            conn.commit()
            updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return jsonify({"job": job_to_dict(updated, read_profile(app)), "message": f"Marked {status}."})

    @app.post("/api/jobs/<int:job_id>/close")
    def close_job(job_id: int) -> Any:
        now = utc_now()
        with closing(get_db(app)) as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not job:
                return jsonify({"error": "Job not found."}), 404
            conn.execute("UPDATE jobs SET status = 'closed', closed_at = ?, updated_at = ? WHERE id = ?", (now, now, job_id))
            conn.execute("DELETE FROM applications WHERE job_id = ?", (job_id,))
            conn.commit()
            updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return jsonify({"job": job_to_dict(updated, read_profile(app)), "message": "Opportunity closed and removed from the application queue."})

    @app.post("/api/jobs/<int:job_id>/reopen")
    def reopen_job(job_id: int) -> Any:
        now = utc_now()
        with closing(get_db(app)) as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not job:
                return jsonify({"error": "Job not found."}), 404
            conn.execute("UPDATE jobs SET status = 'new', closed_at = NULL, updated_at = ? WHERE id = ?", (now, job_id))
            conn.commit()
            updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return jsonify({"job": job_to_dict(updated, read_profile(app)), "message": "Opportunity restored to the new-review queue."})

    @app.post("/api/jobs/discover")
    def discover_jobs() -> Any:
        data = request_json_object()
        query = plain_text(data.get("query") or "qa test engineer", 120)
        market = plain_text(data.get("market") or "India", 120) or "India"
        role_track = plain_text(data.get("role_track"), 80)
        if role_track and role_track not in ROLE_TRACKS:
            return jsonify({"error": "Choose a supported QA role track."}), 400
        enabled_sources = data.get("sources")
        if not isinstance(enabled_sources, list):
            enabled_sources = []
        enabled_sources = {plain_text(item, 80) for item in enabled_sources if plain_text(item, 80)}
        include_product_boards = truthy(data.get("include_product_boards", True))
        product_only = truthy(data.get("product_only"))
        salary_only = truthy(data.get("salary_only"))
        force_refresh = truthy(data.get("force_refresh"))
        cache_key = job_search_cache_key(query, market, role_track, product_only, salary_only, enabled_sources)
        cached = read_job_search_cache(app, cache_key)
        if cached and not force_refresh:
            try:
                previous = datetime.fromisoformat(str(cached["checked_at"]))
                if datetime.now(timezone.utc) - previous < timedelta(minutes=15):
                    return jsonify(
                        {
                            "added": 0,
                            "reviewed": int(cached.get("result_count") or 0),
                            "checked_at": cached["checked_at"],
                            "source_report": cached.get("source_report") or [],
                            "cached": True,
                            "message": f"Showing saved {market} results from {cached['checked_at']}. Your previously discovered roles remain in the inbox.",
                        }
                    )
            except ValueError:
                pass
        try:
            candidates, source_report = discover_qa_jobs(
                query,
                include_product_boards,
                enabled_sources or None,
                market,
            )
        except Exception as exc:
            if cached:
                return jsonify(
                    {
                        "added": 0,
                        "reviewed": int(cached.get("result_count") or 0),
                        "checked_at": cached["checked_at"],
                        "source_report": cached.get("source_report") or [],
                        "cached": True,
                        "message": "Live sources were unavailable, so your saved results are still available in the inbox.",
                    }
                )
            return jsonify({"error": f"Job discovery could not start: {plain_text(exc, 180)}"}), 502
        if role_track and role_track != "All QA tracks":
            candidates = [item for item in candidates if item.get("role_track") == role_track]
        if product_only:
            candidates = [item for item in candidates if item.get("is_product_company")]
        if salary_only:
            candidates = [item for item in candidates if item.get("salary")]
        added = 0
        reviewed = 0
        for candidate in candidates:
            try:
                _, created = create_job(app, candidate, source="Remotive")
                added += int(created)
                reviewed += 1
            except ValueError:
                continue
        checked_at = utc_now()
        available = sum(1 for item in source_report if item.get("status") == "ok")
        source_errors = [str(item.get("detail") or "") for item in source_report if item.get("status") == "unavailable"]
        write_job_search_cache(
            app,
            cache_key,
            query,
            market,
            role_track,
            product_only,
            salary_only,
            source_report,
            reviewed,
            "; ".join(source_errors[:3]),
        )
        return jsonify(
            {
                "added": added,
                "reviewed": reviewed,
                "checked_at": checked_at,
                "source_report": source_report,
                "cached": False,
                "message": f"Reviewed {reviewed} {market} QA/Test role(s) from {available} available public source(s). {added} new role(s) are ready for your approval.",
            }
        )

    @app.post("/api/jobs/import")
    def import_jobs() -> Any:
        uploaded = request.files.get("file")
        if not uploaded or not uploaded.filename:
            return jsonify({"error": "Choose a .csv or .json job export."}), 400
        filename = secure_filename(uploaded.filename)
        extension = Path(filename).suffix.lower()
        if extension not in {".csv", ".json"}:
            return jsonify({"error": "Job import accepts .csv or .json files."}), 400
        raw = uploaded.read(2_500_001)
        if len(raw) > 2_500_000:
            return jsonify({"error": "The job import exceeds the 2.5 MB limit."}), 413
        try:
            text = raw.decode("utf-8-sig")
            if extension == ".csv":
                rows: Any = list(csv.DictReader(StringIO(text)))
            else:
                parsed = json.loads(text)
                rows = parsed.get("jobs") if isinstance(parsed, dict) else parsed
            if not isinstance(rows, list):
                raise ValueError("The import must contain a list of jobs.")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            return jsonify({"error": f"Could not read this job export: {plain_text(exc, 180)}"}), 422

        source_default = plain_text(request.form.get("source"), 80) or "Imported listing"
        added = 0
        duplicates = 0
        skipped = 0
        errors: list[str] = []
        for raw_row in rows[:500]:
            if not isinstance(raw_row, dict):
                skipped += 1
                continue
            row = {str(key).casefold(): value for key, value in raw_row.items()}
            def value_for(*keys: str) -> Any:
                return next((row[key] for key in keys if key in row and row[key] is not None), "")

            title = plain_text(value_for("title", "job title", "name", "position"), 180)
            if not title:
                skipped += 1
                continue
            source = plain_text(value_for("source", "site", "provider"), 80) or source_default
            link = clean_url(value_for("source_url", "url", "link", "job url", "apply_url"))
            description = plain_text(value_for("description", "job description", "contents", "details"), 30000)
            if len(description) < 30:
                description = f"Imported {title} listing from {source}. Open the original link and paste its complete job description before tailoring your resume."
            external = plain_text(value_for("external_id", "id", "job id", "slug"), 250) or link or f"{source}:{title}:{value_for('company', 'company_name')}"
            payload = {
                "external_id": f"import:{external}"[:250],
                "source": source,
                "source_url": link,
                "title": title,
                "company": value_for("company", "company_name", "employer"),
                "location": value_for("location", "candidate_required_location"),
                "job_type": value_for("job_type", "employment_type", "type"),
                "description": description,
                "salary": value_for("salary", "compensation", "package"),
                "posted_at": value_for("posted_at", "publication_date", "date"),
                "role_track": value_for("role_track", "track") or "Test Engineer",
                "source_note": "Imported locally by the user. Review the original listing before applying.",
            }
            try:
                _, created = create_job(app, payload, source=source)
                if created:
                    added += 1
                else:
                    duplicates += 1
            except ValueError as exc:
                skipped += 1
                if len(errors) < 4:
                    errors.append(plain_text(exc, 150))
        return jsonify(
            {
                "added": added,
                "duplicates": duplicates,
                "skipped": skipped,
                "errors": errors,
                "message": f"Imported {added} new role(s). {duplicates} duplicate(s) were left unchanged.",
            }
        )

    @app.get("/api/applications")
    def list_applications() -> Any:
        with closing(get_db(app)) as conn:
            rows = conn.execute(
                """
                SELECT a.id, a.job_id, a.status, a.notes, a.application_kind,
                       a.contact_name, a.contact_role, a.contact_email, a.contact_phone,
                       a.referral_name, a.referral_contact, a.next_step, a.created_at, a.updated_at,
                       j.title, j.company, j.location, j.source_url, j.source
                FROM applications a JOIN jobs j ON j.id = a.job_id
                ORDER BY a.updated_at DESC, a.id DESC
                """
            ).fetchall()
        return jsonify({"applications": [{key: row[key] for key in row.keys()} for row in rows]})

    @app.post("/api/applications")
    def add_manual_application() -> Any:
        data = request_json_object()
        title = plain_text(data.get("title"), 180)
        if not title:
            return jsonify({"error": "Add the role title to track a manual opportunity."}), 400
        kind = plain_text(data.get("application_kind"), 80) or "Manual application"
        if kind not in {"Manual application", "Walk-in", "Referral", "Recruiter outreach", "Campus drive"}:
            return jsonify({"error": "Choose a supported application source."}), 400
        status = plain_text(data.get("status"), 40).casefold() or "approved"
        if status not in APPLICATION_STATUSES:
            return jsonify({"error": "Choose approved, applied, interview, or offer."}), 400
        description = plain_text(data.get("description"), 30000)
        if len(description) < 30:
            description = (
                f"User-created {kind.lower()} opportunity for {title}. "
                "Add the role requirements, referral context, or walk-in details here before tailoring a resume."
            )
        try:
            job, _ = create_job(
                app,
                {
                    "source": kind,
                    "source_url": data.get("source_url"),
                    "title": title,
                    "company": data.get("company"),
                    "location": data.get("location"),
                    "job_type": data.get("job_type"),
                    "description": description,
                    "role_track": data.get("role_track") or "Test Engineer",
                    "source_note": "Manual application tracker entry created by the user.",
                },
                source=kind,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        now = utc_now()
        with closing(get_db(app)) as conn:
            conn.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", (status, now, job["id"]))
            cursor = conn.execute(
                """
                INSERT INTO applications
                (job_id, status, notes, application_kind, contact_name, contact_role, contact_email, contact_phone,
                 referral_name, referral_contact, next_step, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job["id"],
                    status,
                    plain_text(data.get("notes"), 2000),
                    kind,
                    plain_text(data.get("contact_name"), 160),
                    plain_text(data.get("contact_role"), 160),
                    plain_text(data.get("contact_email"), 180),
                    plain_text(data.get("contact_phone"), 80),
                    plain_text(data.get("referral_name"), 160),
                    plain_text(data.get("referral_contact"), 180),
                    plain_text(data.get("next_step"), 500),
                    now,
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                """
                SELECT a.id, a.job_id, a.status, a.notes, a.application_kind,
                       a.contact_name, a.contact_role, a.contact_email, a.contact_phone,
                       a.referral_name, a.referral_contact, a.next_step, a.created_at, a.updated_at,
                       j.title, j.company, j.location, j.source_url, j.source
                FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.id = ?
                """,
                (cursor.lastrowid,),
            ).fetchone()
        return jsonify({"application": {key: row[key] for key in row.keys()}, "message": "Manual opportunity added to your application pipeline."}), 201

    @app.patch("/api/applications/<int:application_id>")
    def update_application(application_id: int) -> Any:
        data = request_json_object()
        status = str(data.get("status", "")).lower()
        if status not in APPLICATION_STATUSES:
            return jsonify({"error": "Unsupported application status."}), 400
        now = utc_now()
        with closing(get_db(app)) as conn:
            app_row = conn.execute("SELECT * FROM applications WHERE id = ?", (application_id,)).fetchone()
            if not app_row:
                return jsonify({"error": "Application not found."}), 404
            def update_value(key: str, limit: int) -> str:
                return plain_text(data.get(key), limit) if key in data else str(app_row[key] or "")

            notes = update_value("notes", 2000)
            conn.execute(
                """
                UPDATE applications SET status = ?, notes = ?, contact_name = ?, contact_role = ?, contact_email = ?,
                    contact_phone = ?, referral_name = ?, referral_contact = ?, next_step = ?, updated_at = ? WHERE id = ?
                """,
                (
                    status,
                    notes,
                    update_value("contact_name", 160),
                    update_value("contact_role", 160),
                    update_value("contact_email", 180),
                    update_value("contact_phone", 80),
                    update_value("referral_name", 160),
                    update_value("referral_contact", 180),
                    update_value("next_step", 500),
                    now,
                    application_id,
                ),
            )
            conn.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", (status, now, app_row["job_id"]))
            conn.commit()
        return jsonify({"message": "Application updated."})

    @app.get("/api/resumes")
    def list_resume_versions() -> Any:
        with closing(get_db(app)) as conn:
            rows = conn.execute(
                """
                SELECT rv.id, rv.job_id, rv.title, rv.filename, rv.created_at,
                       j.company, j.status AS job_status
                FROM resume_versions rv LEFT JOIN jobs j ON j.id = rv.job_id
                ORDER BY rv.created_at DESC, rv.id DESC LIMIT 30
                """
            ).fetchall()
        return jsonify({"resumes": [{key: row[key] for key in row.keys()} for row in rows]})

    @app.get("/api/resume-layouts")
    def resume_layouts_api() -> Any:
        return jsonify(
            {
                "layouts": available_resume_layouts(),
                "notice": "All built-in layouts are original, single-column Word layouts intended for reliable ATS text extraction.",
            }
        )

    @app.get("/api/resumes/starter")
    def download_starter_resume() -> Any:
        profile = normalise_profile(qa_starter_profile())
        analysis = match_profile_to_job(
            profile,
            "QA Test Engineer. Manual testing, automation, API testing, regression testing, SQL, Selenium and Agile delivery.",
            "QA / Test Engineer",
        )
        resume = build_tailored_resume(profile, analysis, "QA / Test Engineer")
        document = build_resume_document(resume, "QA / Test Engineer", "")
        return send_file(
            document,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            as_attachment=True,
            download_name="QA_Test_Engineer_Starter_Resume.docx",
        )

    def generate_resume_response(
        profile: dict[str, Any],
        analysis: dict[str, Any],
        title: str,
        company: str,
        job_id: int | None = None,
        layout: str = "classic",
    ) -> Any:
        tailored = build_tailored_resume(profile, analysis, title)
        filename = resume_filename(profile, title)
        document = build_resume_document(tailored, title, company, layout)
        now = utc_now()
        with closing(get_db(app)) as conn:
            cursor = conn.execute(
                """
                INSERT INTO resume_versions (job_id, title, filename, profile_snapshot, tailored_snapshot, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    title or "General QA Resume",
                    filename,
                    json.dumps(profile, ensure_ascii=False),
                    json.dumps({"resume": tailored, "analysis": analysis, "layout": layout}, ensure_ascii=False),
                    now,
                ),
            )
            version_id = cursor.lastrowid
            conn.commit()
        response = send_file(
            document,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            as_attachment=True,
            download_name=filename,
        )
        response.headers["X-Resume-Version"] = str(version_id)
        return response

    @app.post("/api/resumes/generate")
    def generate_resume() -> Any:
        data = request_json_object()
        profile = read_profile(app)
        job_id = data.get("job_id")
        title = plain_text(data.get("title"), 180)
        company = plain_text(data.get("company"), 180)
        description = plain_text(data.get("description"), 30000)
        layout = plain_text(data.get("layout"), 40) or "classic"
        if layout not in RESUME_LAYOUT_IDS:
            return jsonify({"error": "Choose one of the available ATS-safe Word layouts."}), 400
        if job_id:
            try:
                row = query_job(app, int(job_id))
            except (ValueError, TypeError):
                return jsonify({"error": "Invalid job identifier."}), 400
            # A saved job is useful for version history, but a user may refine
            # the currently visible draft before exporting. Prefer that factual
            # current form when it contains a usable description.
            title = title or row["title"]
            company = company or row["company"]
            description = description or row["description"]
        if len(description) < 30:
            return jsonify({"error": "Analyse or select a job before creating a tailored Word resume."}), 400
        if has_starter_placeholders(profile):
            return jsonify(
                {"error": "Replace the visible starter placeholders with your own factual details before exporting a personalised resume. You can download the starter template from Master profile."}
            ), 422
        if not profile["full_name"] or not profile["experience"]:
            return jsonify({"error": "Add your name and at least one factual experience entry before exporting."}), 400
        analysis = match_profile_to_job(profile, description, title)
        return generate_resume_response(profile, analysis, title, company, int(job_id) if job_id else None, layout)

    @app.get("/api/resumes/<int:version_id>/download")
    def download_resume_version(version_id: int) -> Any:
        with closing(get_db(app)) as conn:
            row = conn.execute("SELECT * FROM resume_versions WHERE id = ?", (version_id,)).fetchone()
        if not row:
            abort(404, description="Resume version not found")
        profile = normalise_profile(json_value(row["profile_snapshot"], DEFAULT_PROFILE))
        snapshot = json_value(row["tailored_snapshot"], {})
        resume = snapshot.get("resume") or build_tailored_resume(profile, snapshot.get("analysis") or {}, row["title"])
        document = build_resume_document(resume, row["title"], "", plain_text(snapshot.get("layout"), 40) or "classic")
        return send_file(
            document,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            as_attachment=True,
            download_name=row["filename"],
        )

    @app.post("/api/resume-text")
    def import_resume_text() -> Any:
        uploaded = request.files.get("file")
        if not uploaded or not uploaded.filename:
            return jsonify({"error": "Choose a .docx, .pdf, or .txt file."}), 400
        filename = secure_filename(uploaded.filename)
        extension = Path(filename).suffix.lower()
        if extension not in ALLOWED_UPLOADS:
            return jsonify({"error": "Only .docx, .pdf, and .txt files are supported."}), 400
        data = uploaded.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            return jsonify({"error": "The file exceeds the 8 MB limit."}), 413
        try:
            text = extract_text_from_bytes(filename, data)
        except Exception:
            return jsonify({"error": "This document could not be read. Try a text-based PDF or DOCX."}), 422
        return jsonify({"filename": filename, "text": plain_text(text, 30000)})

    @app.get("/api/dashboard")
    def dashboard_api() -> Any:
        profile = read_profile(app)
        with closing(get_db(app)) as conn:
            status_rows = conn.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
            latest_rows = conn.execute("SELECT * FROM jobs WHERE status != 'closed' ORDER BY updated_at DESC, id DESC LIMIT 4").fetchall()
            version_count = conn.execute("SELECT COUNT(*) AS count FROM resume_versions").fetchone()["count"]
        status_counts = {row["status"]: row["count"] for row in status_rows}
        return jsonify(
            {
                "profile": profile,
                "completion": calculate_profile_completion(profile),
                "metrics": {
                    "new_jobs": status_counts.get("new", 0),
                    "approved": status_counts.get("approved", 0),
                    "applied": status_counts.get("applied", 0),
                    "resume_versions": version_count,
                },
                "latest_jobs": [job_to_dict(row, profile) for row in latest_rows],
            }
        )

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_: RequestEntityTooLarge) -> Any:
        return jsonify({"error": "Upload exceeds the 8 MB limit."}), 413

    @app.errorhandler(ApiInputError)
    def invalid_api_input(error: ApiInputError) -> Any:
        return jsonify({"error": str(error)}), 400

    @app.errorhandler(404)
    def not_found(error: Exception) -> Any:
        if request.path.startswith("/api/"):
            return jsonify({"error": getattr(error, "description", "Not found")}), 404
        return render_template("error.html", page="", code=404, message="That page does not exist."), 404

    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    debug = os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes"}
    app.run(host="127.0.0.1", port=port, debug=debug)
