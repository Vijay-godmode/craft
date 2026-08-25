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
import hmac
from io import BytesIO, StringIO
import html
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from threading import Lock
from uuid import uuid4
from typing import Any
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_login import LoginManager, current_user, login_user, logout_user
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from auth_service import (
    AccountValidationError,
    authenticate_user,
    change_password,
    create_user,
    load_user,
    record_lockout,
)
from ats_engine import (
    build_tailored_resume,
    calculate_profile_completion,
    match_profile_to_job,
)
from docx_builder import available_resume_layouts, build_resume_document, resume_filename
from job_discovery import ROLE_TRACKS, discover_qa_jobs, source_catalogue
from local_ai import DEFAULT_MODEL, local_ai_review, ollama_status, pull_local_model, start_ollama_service, valid_model_name
from lab_service import LAB_SCENARIOS, SYNTHETIC_CATALOG, money_from_paise, normalise_order_payload, openapi_document, public_scenarios, scenario_by_slug
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
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
AUTH_PUBLIC_ENDPOINTS = {"sign_in", "sign_up", "auth_register", "auth_login", "csrf_api", "health", "not_found"}


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


def current_owner_id() -> int:
    """Return the authenticated workspace owner, never a browser-supplied ID."""

    if not current_user.is_authenticated:
        abort(401, description="Sign in is required for this workspace.")
    return int(current_user.get_id())


def has_role(app: Flask, *roles: str) -> bool:
    if not current_user.is_authenticated:
        return False
    with closing(get_db(app)) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id
            WHERE ur.user_id = ? AND r.code IN ({}) LIMIT 1
            """.format(",".join("?" for _ in roles)),
            (current_owner_id(), *roles),
        ).fetchone()
    return bool(row)


def require_role(app: Flask, *roles: str) -> None:
    if not has_role(app, *roles):
        abort(403, description="This action requires the " + "/".join(roles) + " role.")


def audit(app: Flask, action: str, entity_type: str, entity_id: Any = "", detail: dict[str, Any] | None = None, subject_user_id: int | None = None) -> None:
    """Persist security-relevant business actions without secrets or passwords."""
    with closing(get_db(app)) as conn:
        conn.execute(
            """
            INSERT INTO audit_logs (actor_id, subject_user_id, action, entity_type, entity_id, detail_json, request_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (current_owner_id() if current_user.is_authenticated else None, subject_user_id, action, entity_type, str(entity_id)[:100], json.dumps(detail or {}, ensure_ascii=False), request.environ.get("REQUEST_ID", ""), utc_now()),
        )
        conn.commit()


def csrf_token() -> str:
    """Create one per-session CSRF secret for forms and JSON mutations."""

    token = session.get("careercraft_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["careercraft_csrf"] = token
    return str(token)


def has_valid_csrf_token() -> bool:
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    expected = session.get("careercraft_csrf") or ""
    return bool(supplied and expected and hmac.compare_digest(str(supplied), str(expected)))


def safe_next_path(value: Any) -> str:
    candidate = str(value or "").strip()
    parsed = urlparse(candidate)
    if not candidate.startswith("/") or candidate.startswith("//") or parsed.scheme or parsed.netloc:
        return "/"
    return candidate


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

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                is_active INTEGER NOT NULL DEFAULT 1,
                failed_login_count INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT,
                last_login_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS permissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS role_permissions (
                role_id INTEGER NOT NULL,
                permission_id INTEGER NOT NULL,
                PRIMARY KEY(role_id, permission_id),
                FOREIGN KEY(role_id) REFERENCES roles(id) ON DELETE CASCADE,
                FOREIGN KEY(permission_id) REFERENCES permissions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_roles (
                user_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(user_id, role_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(role_id) REFERENCES roles(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                website TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(owner_id, name),
                FOREIGN KEY(owner_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS company_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                created_by INTEGER NOT NULL,
                title TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT '',
                employment_type TEXT NOT NULL DEFAULT 'Full-time',
                description TEXT NOT NULL,
                required_skills TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'open',
                closes_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE,
                FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS posting_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                posting_id INTEGER NOT NULL,
                candidate_id INTEGER NOT NULL,
                workspace_job_id INTEGER,
                status TEXT NOT NULL DEFAULT 'submitted',
                cover_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(posting_id, candidate_id),
                FOREIGN KEY(posting_id) REFERENCES company_jobs(id) ON DELETE CASCADE,
                FOREIGN KEY(candidate_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(workspace_job_id) REFERENCES jobs(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS interviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                interviewer_id INTEGER,
                scheduled_at TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL CHECK(duration_minutes BETWEEN 15 AND 240),
                mode TEXT NOT NULL DEFAULT 'Video',
                meeting_url TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'scheduled',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(application_id) REFERENCES applications(id) ON DELETE CASCADE,
                FOREIGN KEY(owner_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(interviewer_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS interview_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                interview_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                recommendation TEXT NOT NULL CHECK(recommendation IN ('strong_yes', 'yes', 'neutral', 'no', 'strong_no')),
                rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                feedback TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(interview_id, author_id),
                FOREIGN KEY(interview_id) REFERENCES interviews(id) ON DELETE CASCADE,
                FOREIGN KEY(author_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                link TEXT NOT NULL DEFAULT '',
                read_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER,
                subject_user_id INTEGER,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL DEFAULT '',
                detail_json TEXT NOT NULL DEFAULT '{}',
                request_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(actor_id) REFERENCES users(id) ON DELETE SET NULL,
                FOREIGN KEY(subject_user_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS feature_flags (
                key TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                admin_only INTEGER NOT NULL DEFAULT 1,
                updated_by INTEGER,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(updated_by) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id INTEGER PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS job_search_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                market TEXT NOT NULL,
                role_track TEXT NOT NULL,
                filters_json TEXT NOT NULL DEFAULT '{}',
                source_report TEXT NOT NULL DEFAULT '[]',
                reviewed_count INTEGER NOT NULL DEFAULT 0,
                created_count INTEGER NOT NULL DEFAULT 0,
                existing_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'complete',
                checked_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS job_search_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_run_id INTEGER NOT NULL,
                job_id INTEGER NOT NULL,
                rank INTEGER NOT NULL,
                result_state TEXT NOT NULL DEFAULT 'existing',
                UNIQUE(search_run_id, job_id),
                FOREIGN KEY(search_run_id) REFERENCES job_search_runs(id) ON DELETE CASCADE,
                FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS lab_catalog_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                sku TEXT NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                price_paise INTEGER NOT NULL CHECK(price_paise >= 0),
                stock INTEGER NOT NULL CHECK(stock >= 0),
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, sku),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS lab_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                order_number TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'created',
                total_paise INTEGER NOT NULL CHECK(total_paise >= 0),
                idempotency_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, idempotency_key),
                UNIQUE(user_id, order_number),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS lab_order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                unit_price_paise INTEGER NOT NULL CHECK(unit_price_paise >= 0),
                FOREIGN KEY(order_id) REFERENCES lab_orders(id) ON DELETE CASCADE,
                FOREIGN KEY(product_id) REFERENCES lab_catalog_items(id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS qa_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                scenario_slug TEXT NOT NULL,
                suite TEXT NOT NULL,
                status TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                summary_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_versions_created ON resume_versions(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
            CREATE INDEX IF NOT EXISTS idx_search_runs_owner ON job_search_runs(user_id, checked_at DESC);
            CREATE INDEX IF NOT EXISTS idx_search_results_run ON job_search_results(search_run_id, rank);
            CREATE INDEX IF NOT EXISTS idx_lab_catalog_owner ON lab_catalog_items(user_id, category);
            CREATE INDEX IF NOT EXISTS idx_lab_orders_owner ON lab_orders(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_qa_runs_owner ON qa_runs(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_user_roles_role ON user_roles(role_id, user_id);
            CREATE INDEX IF NOT EXISTS idx_companies_owner ON companies(owner_id, status);
            CREATE INDEX IF NOT EXISTS idx_company_jobs_status ON company_jobs(status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_posting_apps_posting ON posting_applications(posting_id, status);
            CREATE INDEX IF NOT EXISTS idx_interviews_owner_schedule ON interviews(owner_id, scheduled_at);
            CREATE INDEX IF NOT EXISTS idx_notifications_owner ON notifications(user_id, read_at, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_logs_entity ON audit_logs(entity_type, entity_id, created_at DESC);
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
        if "user_id" not in existing_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN user_id INTEGER")
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
        if "user_id" not in existing_application_columns:
            conn.execute("ALTER TABLE applications ADD COLUMN user_id INTEGER")
        existing_version_columns = {row["name"] for row in conn.execute("PRAGMA table_info(resume_versions)").fetchall()}
        if "user_id" not in existing_version_columns:
            conn.execute("ALTER TABLE resume_versions ADD COLUMN user_id INTEGER")
        existing_search_columns = {row["name"] for row in conn.execute("PRAGMA table_info(job_searches)").fetchall()}
        if "user_id" not in existing_search_columns:
            conn.execute("ALTER TABLE job_searches ADD COLUMN user_id INTEGER")
        if "search_run_id" not in existing_search_columns:
            conn.execute("ALTER TABLE job_searches ADD COLUMN search_run_id INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_track ON jobs(role_track)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_owner_status ON jobs(user_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_applications_owner ON applications(user_id, updated_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_versions_owner ON resume_versions(user_id, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_job_searches_updated ON job_searches(updated_at DESC)")
        now = utc_now()
        conn.executemany(
            "INSERT OR IGNORE INTO roles (code, label, created_at) VALUES (?, ?, ?)",
            [("candidate", "Candidate", now), ("recruiter", "Recruiter", now), ("hiring_manager", "Hiring Manager", now), ("admin", "Administrator", now)],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO permissions (code, description, created_at) VALUES (?, ?, ?)",
            [
                ("candidate.self", "Manage own candidate workspace", now),
                ("jobs.manage", "Create and manage company jobs", now),
                ("applications.manage", "Manage candidate applications", now),
                ("interviews.manage", "Schedule interviews", now),
                ("admin.manage", "Manage QA controls and roles", now),
            ],
        )
        for user in conn.execute("SELECT id, role, created_at FROM users").fetchall():
            code = str(user["role"] or "candidate")
            if code == "user":
                code = "candidate"
                conn.execute("UPDATE users SET role = ? WHERE id = ?", (code, user["id"]))
            role = conn.execute("SELECT id FROM roles WHERE code = ?", (code,)).fetchone()
            if role:
                conn.execute("INSERT OR IGNORE INTO user_roles (user_id, role_id, created_at) VALUES (?, ?, ?)", (user["id"], role["id"], user["created_at"]))
        conn.commit()


def claim_legacy_workspace(app: Flask, user_id: int) -> None:
    """Assign the pre-auth single-user data to the first local account once.

    Older CareerCraft builds kept one shared profile and unowned records.  The
    first account created after this upgrade is the only safe candidate to own
    that local workspace.  Later registrations begin with an empty workspace.
    """

    with closing(get_db(app)) as conn:
        legacy_profile = conn.execute("SELECT data, updated_at FROM profile WHERE id = 1").fetchone()
        if legacy_profile and not conn.execute("SELECT 1 FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone():
            conn.execute(
                "INSERT INTO user_profiles (user_id, data, updated_at) VALUES (?, ?, ?)",
                (user_id, legacy_profile["data"], legacy_profile["updated_at"]),
            )
        conn.execute("UPDATE jobs SET user_id = ? WHERE user_id IS NULL", (user_id,))
        conn.execute("UPDATE applications SET user_id = ? WHERE user_id IS NULL", (user_id,))
        conn.execute("UPDATE resume_versions SET user_id = ? WHERE user_id IS NULL", (user_id,))
        conn.execute("UPDATE job_searches SET user_id = ? WHERE user_id IS NULL", (user_id,))
        # Prefix old identifiers once so an identical public role may be saved
        # independently by a future account without violating the old global
        # SQLite UNIQUE constraint.
        legacy_jobs = conn.execute("SELECT id, external_id FROM jobs WHERE user_id = ? AND external_id IS NOT NULL", (user_id,)).fetchall()
        for row in legacy_jobs:
            if not str(row["external_id"]).startswith(f"u{user_id}:"):
                conn.execute("UPDATE jobs SET external_id = ? WHERE id = ?", (f"u{user_id}:{row['external_id']}"[:250], row["id"]))
        legacy_searches = conn.execute("SELECT cache_key FROM job_searches WHERE user_id = ?", (user_id,)).fetchall()
        for row in legacy_searches:
            if not str(row["cache_key"]).startswith(f"u{user_id}:"):
                conn.execute("UPDATE job_searches SET cache_key = ? WHERE cache_key = ?", (f"u{user_id}:{row['cache_key']}", row["cache_key"]))
        legacy_settings = conn.execute("SELECT key, value, updated_at FROM settings WHERE key NOT LIKE 'user:%'").fetchall()
        for row in legacy_settings:
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (f"user:{user_id}:{row['key']}", row["value"], row["updated_at"]),
            )
        conn.commit()


def read_profile(app: Flask, user_id: int | None = None) -> dict[str, Any]:
    user_id = user_id or current_owner_id()
    with closing(get_db(app)) as conn:
        row = conn.execute("SELECT data FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return normalise_profile(qa_starter_profile())
    return normalise_profile(json_value(row["data"], DEFAULT_PROFILE))


def write_profile(app: Flask, payload: Any, user_id: int | None = None) -> dict[str, Any]:
    user_id = user_id or current_owner_id()
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
            INSERT INTO user_profiles (user_id, data, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at
            """,
            (user_id, json.dumps(profile, ensure_ascii=False), now),
        )
        conn.commit()
    return profile


def setting_storage_key(key: str, user_id: int | None = None) -> str:
    return f"user:{user_id or current_owner_id()}:{key}"


def get_setting(app: Flask, key: str, user_id: int | None = None) -> str | None:
    with closing(get_db(app)) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (setting_storage_key(key, user_id),)).fetchone()
    return row["value"] if row else None


def set_setting(app: Flask, key: str, value: str, user_id: int | None = None) -> None:
    storage_key = setting_storage_key(key, user_id)
    now = utc_now()
    with closing(get_db(app)) as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (storage_key, value, now),
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
    user_id: int | None = None,
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
    return f"u{user_id or current_owner_id()}:{sha256(payload.encode('utf-8')).hexdigest()}"


def read_job_search_cache(app: Flask, cache_key: str) -> dict[str, Any] | None:
    user_id = current_owner_id()
    with closing(get_db(app)) as conn:
        row = conn.execute("SELECT * FROM job_searches WHERE cache_key = ? AND user_id = ?", (cache_key, user_id)).fetchone()
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
    search_run_id: int | None = None,
) -> None:
    user_id = current_owner_id()
    now = utc_now()
    with closing(get_db(app)) as conn:
        conn.execute(
            """
            INSERT INTO job_searches
            (cache_key, user_id, query, market, role_track, product_only, salary_only, source_report, checked_at, result_count, last_error, updated_at, search_run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                user_id = excluded.user_id,
                source_report = excluded.source_report,
                checked_at = excluded.checked_at,
                result_count = excluded.result_count,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at,
                search_run_id = excluded.search_run_id
            """,
            (
                cache_key,
                user_id,
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
                search_run_id,
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
    user_id = current_owner_id()
    with closing(get_db(app)) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
    if not row:
        abort(404, description="Job not found")
    return row


def create_job(app: Flask, payload: dict[str, Any], source: str = "Manual") -> tuple[dict[str, Any], bool]:
    user_id = current_owner_id()
    title = plain_text(payload.get("title"), 180) or "QA opportunity"
    description = plain_text(payload.get("description"), 30000)
    if len(description) < 30:
        raise ValueError("Paste a fuller job description (at least 30 characters).")
    source_external_id = plain_text(payload.get("external_id"), 230) or None
    # Older versions made external_id globally unique.  Prefixing it with the
    # authenticated owner preserves independent private inboxes without a
    # risky table rebuild on existing local installations.
    external_id = f"u{user_id}:{source_external_id}" if source_external_id else None
    try:
        quality_score = min(99, max(0, int(payload.get("quality_score") or 0)))
    except (TypeError, ValueError):
        quality_score = 0
    now = utc_now()
    values = (
        user_id,
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
                (user_id, external_id, source, source_url, title, company, location, job_type, description,
                 salary, posted_at, role_track, quality_score, company_signal, is_product_company, source_note,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            job_id = cursor.lastrowid
            conn.commit()
            created = True
        except sqlite3.IntegrityError:
            if not external_id:
                raise
            row = conn.execute("SELECT * FROM jobs WHERE external_id = ? AND user_id = ?", (external_id, user_id)).fetchone()
            if not row:
                raise
            job_id = row["id"]
            created = False
        row = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
    return job_to_dict(row, read_profile(app, user_id)), created


def search_run_results(app: Flask, search_run_id: int) -> list[dict[str, Any]]:
    """Return the exact records produced by one owned discovery refresh."""

    user_id = current_owner_id()
    with closing(get_db(app)) as conn:
        rows = conn.execute(
            """
            SELECT jsr.rank, jsr.result_state, j.*
            FROM job_search_results jsr
            JOIN job_search_runs run ON run.id = jsr.search_run_id
            JOIN jobs j ON j.id = jsr.job_id
            WHERE jsr.search_run_id = ? AND run.user_id = ? AND j.user_id = ?
            ORDER BY jsr.rank ASC, jsr.id ASC
            """,
            (search_run_id, user_id, user_id),
        ).fetchall()
    profile = read_profile(app, user_id)
    results: list[dict[str, Any]] = []
    for row in rows:
        result = job_to_dict(row, profile)
        result["search_rank"] = int(row["rank"])
        result["search_result_state"] = str(row["result_state"])
        results.append(result)
    return results


def search_run_summary(app: Flask, search_run_id: int) -> dict[str, Any] | None:
    user_id = current_owner_id()
    with closing(get_db(app)) as conn:
        row = conn.execute(
            "SELECT * FROM job_search_runs WHERE id = ? AND user_id = ?", (search_run_id, user_id)
        ).fetchone()
    if not row:
        return None
    result = {key: row[key] for key in row.keys()}
    result["source_report"] = json_value(result.get("source_report"), [])
    result["filters"] = json_value(result.get("filters_json"), {})
    return result


def ensure_lab_catalog(app: Flask, user_id: int) -> None:
    """Seed each private QA Lab with deterministic, non-sensitive data once."""

    with closing(get_db(app)) as conn:
        existing = conn.execute("SELECT COUNT(*) AS count FROM lab_catalog_items WHERE user_id = ?", (user_id,)).fetchone()["count"]
        if existing:
            return
        now = utc_now()
        conn.executemany(
            """
            INSERT INTO lab_catalog_items (user_id, sku, name, category, price_paise, stock, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            [
                (user_id, item["sku"], item["name"], item["category"], item["price_paise"], item["stock"], now, now)
                for item in SYNTHETIC_CATALOG
            ],
        )
        conn.commit()


def lab_catalog_item_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "sku": row["sku"],
        "name": row["name"],
        "category": row["category"],
        "price": money_from_paise(int(row["price_paise"])),
        "price_paise": int(row["price_paise"]),
        "stock": int(row["stock"]),
        "active": bool(row["active"]),
    }


def lab_order_to_dict(conn: sqlite3.Connection, order_id: int, user_id: int) -> dict[str, Any] | None:
    order = conn.execute("SELECT * FROM lab_orders WHERE id = ? AND user_id = ?", (order_id, user_id)).fetchone()
    if not order:
        return None
    item_rows = conn.execute(
        """
        SELECT oi.product_id, oi.quantity, oi.unit_price_paise, ci.sku, ci.name
        FROM lab_order_items oi JOIN lab_catalog_items ci ON ci.id = oi.product_id
        WHERE oi.order_id = ? AND ci.user_id = ?
        ORDER BY oi.id ASC
        """,
        (order_id, user_id),
    ).fetchall()
    return {
        "id": int(order["id"]),
        "order_number": order["order_number"],
        "customer_name": order["customer_name"],
        "status": order["status"],
        "total": money_from_paise(int(order["total_paise"])),
        "total_paise": int(order["total_paise"]),
        "created_at": order["created_at"],
        "items": [
            {
                "product_id": int(item["product_id"]),
                "sku": item["sku"],
                "name": item["name"],
                "quantity": int(item["quantity"]),
                "unit_price": money_from_paise(int(item["unit_price_paise"])),
                "unit_price_paise": int(item["unit_price_paise"]),
            }
            for item in item_rows
        ],
    }


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.from_mapping(
        # A local run gets an ephemeral secret instead of a published default.
        # Set RESUME_SECRET_KEY explicitly whenever a deployment needs stable
        # signed sessions or runs more than one process.
        SECRET_KEY=os.environ.get("RESUME_SECRET_KEY") or os.urandom(32),
        DATABASE=os.environ.get("RESUME_DB_PATH", str(DEFAULT_DB_PATH)),
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=truthy(os.environ.get("RESUME_SESSION_SECURE")),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        LOCAL_CODE_ASSISTANT=truthy(os.environ.get("LOCAL_CODE_ASSISTANT", "1")),
    )
    if test_config:
        app.config.update(test_config)
    init_db(app)

    login_manager = LoginManager()
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_logged_in_user(user_id: str) -> Any:
        with closing(get_db(app)) as conn:
            return load_user(conn, user_id)

    @app.before_request
    def require_account_and_csrf() -> Any:
        # Static assets, health checks, and account bootstrap are intentionally
        # reachable before sign-in. Every resume/workspace route is private.
        if request.path.startswith("/static/") or request.endpoint in AUTH_PUBLIC_ENDPOINTS:
            if request.method in MUTATING_METHODS and request.endpoint in {"auth_register", "auth_login"} and not has_valid_csrf_token():
                return jsonify({"error": "Your security token is missing or expired. Refresh the page and try again."}), 403
            return None
        if not current_user.is_authenticated:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Sign in is required for this workspace."}), 401
            return redirect(url_for("sign_in", next=safe_next_path(request.full_path)))
        if request.method in MUTATING_METHODS and not has_valid_csrf_token():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Your security token is missing or expired. Refresh the page and try again."}), 403
            abort(403, description="Your security token is missing or expired.")
        return None

    @app.after_request
    def add_security_headers(response: Any) -> Any:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
        )
        if request.path.startswith(("/sign-", "/account")):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.context_processor
    def inject_navigation() -> dict[str, Any]:
        return {"app_name": "CareerCraft", "csrf_token": csrf_token()}

    @app.get("/sign-in")
    def sign_in() -> Any:
        if current_user.is_authenticated:
            return redirect("/")
        return render_template("sign_in.html", page="", next_path=safe_next_path(request.args.get("next")))

    @app.get("/sign-up")
    def sign_up() -> Any:
        if current_user.is_authenticated:
            return redirect("/")
        return render_template("sign_up.html", page="", next_path=safe_next_path(request.args.get("next")))

    @app.get("/api/csrf")
    def csrf_api() -> Any:
        return jsonify({"csrf_token": csrf_token()})

    @app.post("/api/auth/register")
    def auth_register() -> Any:
        data = request_json_object()
        now = utc_now()
        with closing(get_db(app)) as conn:
            is_first_account = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"] == 0
            try:
                user = create_user(
                    conn,
                    email=data.get("email"),
                    password=data.get("password"),
                    display_name=data.get("display_name"),
                    created_at=now,
                    role="admin" if is_first_account else "candidate",
                )
            except AccountValidationError as exc:
                return jsonify({"error": str(exc)}), 400
            conn.commit()
        with closing(get_db(app)) as conn:
            role = conn.execute("SELECT id FROM roles WHERE code = ?", (user.role,)).fetchone()
            if role:
                conn.execute("INSERT OR IGNORE INTO user_roles (user_id, role_id, created_at) VALUES (?, ?, ?)", (user.id, role["id"], now))
                conn.commit()
        if is_first_account:
            claim_legacy_workspace(app, user.id)
        session.clear()
        login_user(user, remember=False, fresh=True)
        return jsonify({"user": user.public(), "csrf_token": csrf_token(), "message": "Your private CareerCraft workspace is ready."}), 201

    @app.post("/api/auth/login")
    def auth_login() -> Any:
        data = request_json_object()
        now = utc_now()
        with closing(get_db(app)) as conn:
            user, state = authenticate_user(conn, data.get("email"), data.get("password"), now)
            if state == "invalid":
                record_lockout(conn, data.get("email"), (datetime.now(timezone.utc) + timedelta(minutes=10)).replace(microsecond=0).isoformat(), now)
            conn.commit()
        if state == "locked":
            return jsonify({"error": "This account is temporarily locked. Try again in a few minutes."}), 429
        if not user:
            return jsonify({"error": "Email or password is not correct."}), 401
        session.clear()
        login_user(user, remember=False, fresh=True)
        return jsonify({"user": user.public(), "csrf_token": csrf_token(), "message": "Signed in successfully."})

    @app.post("/api/auth/logout")
    def auth_logout() -> Any:
        logout_user()
        session.clear()
        return jsonify({"message": "Signed out."})

    @app.get("/api/auth/me")
    def auth_me() -> Any:
        return jsonify({"authenticated": bool(current_user.is_authenticated), "user": current_user.public() if current_user.is_authenticated else None})

    @app.get("/account")
    def account_page() -> str:
        return render_template("account.html", page="account")

    @app.get("/marketplace")
    def marketplace_page() -> str:
        return render_template("marketplace.html", page="marketplace")

    @app.get("/recruiter")
    def recruiter_page() -> Any:
        require_role(app, "recruiter", "hiring_manager", "admin")
        return render_template("recruiter.html", page="recruiter")

    @app.get("/api/marketplace/jobs")
    def marketplace_jobs() -> Any:
        with closing(get_db(app)) as conn:
            rows = conn.execute("""SELECT cj.*, c.name AS company_name FROM company_jobs cj JOIN companies c ON c.id=cj.company_id WHERE cj.status='open' ORDER BY cj.created_at DESC LIMIT 100""").fetchall()
        return jsonify({"jobs": [{**{key: row[key] for key in row.keys()}, "required_skills": json_value(row["required_skills"], [])} for row in rows]})

    @app.post("/api/recruiter/companies")
    def create_company() -> Any:
        require_role(app, "recruiter", "hiring_manager", "admin")
        data, user_id, now = request_json_object(), current_owner_id(), utc_now()
        name = plain_text(data.get("name"), 180)
        if len(name) < 2:
            return jsonify({"error": "Company name must contain at least two characters."}), 400
        with closing(get_db(app)) as conn:
            cursor = conn.execute("INSERT INTO companies (owner_id,name,website,location,description,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (user_id,name,clean_url(data.get("website")),plain_text(data.get("location"),180),plain_text(data.get("description"),2000),now,now))
            conn.commit()
        audit(app, "company.created", "company", cursor.lastrowid, {"name": name})
        return jsonify({"id": cursor.lastrowid, "message": "Company created."}), 201

    @app.post("/api/recruiter/jobs")
    def create_company_job() -> Any:
        require_role(app, "recruiter", "hiring_manager", "admin")
        data, user_id, now = request_json_object(), current_owner_id(), utc_now()
        title, description = plain_text(data.get("title"),180), plain_text(data.get("description"),30000)
        try: company_id = int(data.get("company_id"))
        except (TypeError, ValueError): return jsonify({"error": "Choose a company."}), 400
        if not title or len(description) < 30: return jsonify({"error": "Add a title and at least 30 characters of job description."}), 400
        with closing(get_db(app)) as conn:
            company = conn.execute("SELECT id FROM companies WHERE id=? AND owner_id=?", (company_id,user_id)).fetchone()
            if not company: return jsonify({"error": "Company not found."}), 404
            cursor = conn.execute("INSERT INTO company_jobs (company_id,created_by,title,location,employment_type,description,required_skills,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (company_id,user_id,title,plain_text(data.get("location"),180),plain_text(data.get("employment_type"),80) or "Full-time",description,json.dumps(list_of_strings(data.get("required_skills"),40)),now,now))
            conn.commit()
        audit(app, "company_job.created", "company_job", cursor.lastrowid, {"title": title})
        return jsonify({"id": cursor.lastrowid, "message": "Job posting opened."}), 201

    @app.get("/api/recruiter/jobs")
    def recruiter_jobs() -> Any:
        require_role(app, "recruiter", "hiring_manager", "admin")
        user_id = current_owner_id()
        with closing(get_db(app)) as conn:
            rows = conn.execute(
                """SELECT cj.*, c.name AS company_name FROM company_jobs cj JOIN companies c ON c.id = cj.company_id
                   WHERE cj.created_by = ? ORDER BY cj.updated_at DESC, cj.id DESC""", (user_id,)
            ).fetchall()
        return jsonify({"jobs": [{**{key: row[key] for key in row.keys()}, "required_skills": json_value(row["required_skills"], [])} for row in rows]})

    @app.patch("/api/recruiter/jobs/<int:posting_id>")
    def update_company_job(posting_id: int) -> Any:
        require_role(app, "recruiter", "hiring_manager", "admin")
        data, user_id, now = request_json_object(), current_owner_id(), utc_now()
        with closing(get_db(app)) as conn:
            posting = conn.execute("SELECT * FROM company_jobs WHERE id = ? AND created_by = ?", (posting_id, user_id)).fetchone()
            if not posting:
                return jsonify({"error": "Job posting not found."}), 404
            title = plain_text(data.get("title"), 180) if "title" in data else posting["title"]
            description = plain_text(data.get("description"), 30000) if "description" in data else posting["description"]
            status = plain_text(data.get("status"), 30).casefold() if "status" in data else posting["status"]
            if not title or len(description) < 30 or status not in {"open", "closed", "paused"}:
                return jsonify({"error": "Use a valid title, description, and open/paused/closed status."}), 400
            conn.execute(
                """UPDATE company_jobs SET title = ?, location = ?, employment_type = ?, description = ?, required_skills = ?, status = ?, closes_at = ?, updated_at = ? WHERE id = ? AND created_by = ?""",
                (title, plain_text(data.get("location"), 180) if "location" in data else posting["location"], plain_text(data.get("employment_type"), 80) if "employment_type" in data else posting["employment_type"], description, json.dumps(list_of_strings(data.get("required_skills"), 40) if "required_skills" in data else json_value(posting["required_skills"], [])), status, now if status == "closed" else posting["closes_at"], now, posting_id, user_id),
            )
            conn.commit()
        audit(app, "company_job.updated", "company_job", posting_id, {"status": status})
        return jsonify({"message": "Job posting updated."})

    @app.get("/api/recruiter/jobs/<int:posting_id>/applications")
    def recruiter_job_applications(posting_id: int) -> Any:
        require_role(app, "recruiter", "hiring_manager", "admin")
        user_id = current_owner_id()
        with closing(get_db(app)) as conn:
            rows = conn.execute(
                """SELECT pa.*, cj.title, u.email AS candidate_email, u.display_name AS candidate_name
                   FROM posting_applications pa JOIN company_jobs cj ON cj.id = pa.posting_id JOIN users u ON u.id = pa.candidate_id
                   WHERE pa.posting_id = ? AND cj.created_by = ? ORDER BY pa.created_at DESC""", (posting_id, user_id)
            ).fetchall()
        if not rows:
            with closing(get_db(app)) as conn:
                exists = conn.execute("SELECT 1 FROM company_jobs WHERE id = ? AND created_by = ?", (posting_id, user_id)).fetchone()
            if not exists:
                return jsonify({"error": "Job posting not found."}), 404
        return jsonify({"applications": [{key: row[key] for key in row.keys()} for row in rows]})

    @app.patch("/api/recruiter/applications/<int:application_id>")
    def recruiter_update_application(application_id: int) -> Any:
        require_role(app, "recruiter", "hiring_manager", "admin")
        data, user_id, now = request_json_object(), current_owner_id(), utc_now()
        status = plain_text(data.get("status"), 40).casefold()
        if status not in {"submitted", "screening", "interview", "rejected", "hired"}:
            return jsonify({"error": "Choose submitted, screening, interview, rejected, or hired."}), 400
        with closing(get_db(app)) as conn:
            row = conn.execute("""SELECT pa.* FROM posting_applications pa JOIN company_jobs cj ON cj.id = pa.posting_id
                                  WHERE pa.id = ? AND cj.created_by = ?""", (application_id, user_id)).fetchone()
            if not row:
                return jsonify({"error": "Candidate application not found."}), 404
            conn.execute("UPDATE posting_applications SET status = ?, updated_at = ? WHERE id = ?", (status, now, application_id))
            conn.execute("INSERT INTO notifications (user_id, kind, title, body, link, created_at) VALUES (?, 'application_status', ?, ?, '/applications', ?)", (row["candidate_id"], "Application status updated", f"Your marketplace application is now {status}.", now))
            conn.commit()
        audit(app, "posting_application.updated", "posting_application", application_id, {"status": status}, row["candidate_id"])
        return jsonify({"message": "Candidate application updated."})

    @app.get("/api/notifications")
    def list_notifications() -> Any:
        user_id = current_owner_id()
        with closing(get_db(app)) as conn:
            rows = conn.execute("SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 100", (user_id,)).fetchall()
        return jsonify({"notifications": [{key: row[key] for key in row.keys()} for row in rows]})

    @app.post("/api/notifications/<int:notification_id>/read")
    def mark_notification_read(notification_id: int) -> Any:
        user_id = current_owner_id()
        with closing(get_db(app)) as conn:
            conn.execute("UPDATE notifications SET read_at = ? WHERE id = ? AND user_id = ?", (utc_now(), notification_id, user_id))
            conn.commit()
        return jsonify({"message": "Notification marked as read."})

    @app.get("/api/interviews")
    def list_interviews() -> Any:
        user_id = current_owner_id()
        with closing(get_db(app)) as conn:
            rows = conn.execute(
                """SELECT i.*, j.title, j.company FROM interviews i JOIN applications a ON a.id = i.application_id
                   JOIN jobs j ON j.id = a.job_id WHERE i.owner_id = ? AND a.user_id = ?
                   ORDER BY i.scheduled_at ASC, i.id ASC""", (user_id, user_id)
            ).fetchall()
        return jsonify({"interviews": [{key: row[key] for key in row.keys()} for row in rows]})

    @app.post("/api/interviews")
    def create_interview() -> Any:
        data, user_id, now = request_json_object(), current_owner_id(), utc_now()
        try:
            application_id = int(data.get("application_id"))
            duration = int(data.get("duration_minutes", 30))
        except (TypeError, ValueError):
            return jsonify({"error": "Choose an application and a valid duration."}), 400
        scheduled_at = plain_text(data.get("scheduled_at"), 80)
        if not scheduled_at or not 15 <= duration <= 240:
            return jsonify({"error": "Add a scheduled time and a duration between 15 and 240 minutes."}), 400
        with closing(get_db(app)) as conn:
            application = conn.execute("SELECT id, job_id FROM applications WHERE id = ? AND user_id = ?", (application_id, user_id)).fetchone()
            if not application:
                return jsonify({"error": "Application not found."}), 404
            cursor = conn.execute(
                """INSERT INTO interviews (application_id, owner_id, scheduled_at, duration_minutes, mode, meeting_url, notes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (application_id, user_id, scheduled_at, duration, plain_text(data.get("mode"), 40) or "Video", clean_url(data.get("meeting_url")), plain_text(data.get("notes"), 2000), now, now),
            )
            conn.execute("INSERT INTO notifications (user_id, kind, title, body, link, created_at) VALUES (?, 'interview_scheduled', 'Interview scheduled', ?, '/applications', ?)", (user_id, f"Interview scheduled for {scheduled_at}.", now))
            conn.commit()
        audit(app, "interview.created", "interview", cursor.lastrowid, {"application_id": application_id})
        return jsonify({"id": cursor.lastrowid, "message": "Interview scheduled."}), 201

    @app.patch("/api/interviews/<int:interview_id>")
    def update_interview(interview_id: int) -> Any:
        data, user_id, now = request_json_object(), current_owner_id(), utc_now()
        with closing(get_db(app)) as conn:
            interview = conn.execute("SELECT * FROM interviews WHERE id = ? AND owner_id = ?", (interview_id, user_id)).fetchone()
            if not interview:
                return jsonify({"error": "Interview not found."}), 404
            status = plain_text(data.get("status"), 30).casefold() if "status" in data else interview["status"]
            if status not in {"scheduled", "completed", "cancelled", "rescheduled"}:
                return jsonify({"error": "Choose scheduled, completed, cancelled, or rescheduled."}), 400
            conn.execute("UPDATE interviews SET scheduled_at = ?, duration_minutes = ?, mode = ?, meeting_url = ?, status = ?, notes = ?, updated_at = ? WHERE id = ? AND owner_id = ?", (plain_text(data.get("scheduled_at"), 80) or interview["scheduled_at"], int(data.get("duration_minutes", interview["duration_minutes"])), plain_text(data.get("mode"), 40) or interview["mode"], clean_url(data.get("meeting_url")) if "meeting_url" in data else interview["meeting_url"], status, plain_text(data.get("notes"), 2000) if "notes" in data else interview["notes"], now, interview_id, user_id))
            conn.commit()
        audit(app, "interview.updated", "interview", interview_id, {"status": status})
        return jsonify({"message": "Interview updated."})

    @app.post("/api/marketplace/jobs/<int:posting_id>/apply")
    def apply_marketplace_job(posting_id: int) -> Any:
        user_id, data, now = current_owner_id(), request_json_object(), utc_now()
        with closing(get_db(app)) as conn:
            posting = conn.execute("""SELECT cj.*, c.name AS company_name FROM company_jobs cj JOIN companies c ON c.id=cj.company_id WHERE cj.id=? AND cj.status='open'""", (posting_id,)).fetchone()
            if not posting: return jsonify({"error": "This job posting is not open."}), 404
        try:
            workspace_job, _ = create_job(app, {"external_id": f"marketplace:{posting_id}", "source": "CareerCraft Marketplace", "title": posting["title"], "company": posting["company_name"], "location": posting["location"], "job_type": posting["employment_type"], "description": posting["description"], "source_note": "Applied through the internal QA marketplace."})
        except ValueError as exc: return jsonify({"error": str(exc)}), 400
        with closing(get_db(app)) as conn:
            try:
                cursor=conn.execute("INSERT INTO posting_applications (posting_id,candidate_id,workspace_job_id,cover_note,created_at,updated_at) VALUES (?,?,?,?,?,?)", (posting_id,user_id,workspace_job["id"],plain_text(data.get("cover_note"),2000),now,now))
            except sqlite3.IntegrityError:
                return jsonify({"error": "You have already applied to this posting."}), 409
            conn.execute("UPDATE jobs SET status='applied', updated_at=? WHERE id=? AND user_id=?", (now,workspace_job["id"],user_id))
            conn.execute("INSERT INTO applications (user_id,job_id,status,notes,application_kind,created_at,updated_at) VALUES (?,?, 'applied', ?, 'Marketplace application', ?, ?)", (user_id,workspace_job["id"],plain_text(data.get("cover_note"),2000),now,now))
            conn.commit()
        audit(app, "marketplace.applied", "company_job", posting_id, {"application_id": cursor.lastrowid})
        return jsonify({"id": cursor.lastrowid, "job": workspace_job, "message": "Application submitted and added to your private pipeline."}), 201

    @app.patch("/api/account")
    def update_account() -> Any:
        data = request_json_object()
        user_id = current_owner_id()
        now = utc_now()
        display_name = plain_text(data.get("display_name"), 80) if "display_name" in data else ""
        with closing(get_db(app)) as conn:
            if display_name:
                if len(display_name) < 2:
                    return jsonify({"error": "Enter the name you want CareerCraft to use."}), 400
                conn.execute("UPDATE users SET display_name = ?, updated_at = ? WHERE id = ?", (display_name, now, user_id))
            if data.get("new_password"):
                try:
                    change_password(conn, user_id, data.get("current_password"), data.get("new_password"), now)
                except AccountValidationError as exc:
                    return jsonify({"error": str(exc)}), 400
            conn.commit()
            refreshed = load_user(conn, user_id)
        return jsonify({"user": refreshed.public() if refreshed else current_user.public(), "message": "Account settings saved."})

    @app.get("/lab")
    def lab_overview_page() -> str:
        return render_template("lab_overview.html", page="lab", scenario_count=len(LAB_SCENARIOS))

    @app.get("/lab/workflows")
    def lab_workflows_page() -> str:
        return render_template("lab_workflows.html", page="lab", scenarios=public_scenarios())

    @app.get("/lab/workflows/<slug>")
    def lab_workflow_detail_page(slug: str) -> Any:
        scenario = scenario_by_slug(slug)
        if not scenario:
            abort(404, description="QA Lab scenario not found")
        return render_template("lab_workflow_detail.html", page="lab", scenario=scenario)

    @app.get("/lab/api")
    def lab_api_page() -> str:
        return render_template("lab_api.html", page="lab-api")

    @app.get("/lab/ui")
    def lab_ui_page() -> str:
        return render_template(
            "lab_topic.html",
            page="lab",
            topic={
                "eyebrow": "UI / UX TESTING",
                "title": "Test the real interface, not a mock exercise.",
                "lede": "Use this application to practise functional UI, responsive, accessibility, and compatibility testing against changing state.",
                "checks": ["Jobs refresh: loading, latest results, empty, provider failure, and tab-filter states.", "Profile form: labels, keyboard navigation, validation, imported draft review, and save feedback.", "Applications: expandable recruiter details, status changes, close/reopen, and narrow viewports.", "Automation target: use semantic roles first; data-testid attributes are added only where a stable selector is truly needed."],
                "links": [("Open jobs scenario", "/lab/workflows/integration-discovery"), ("Open accessibility scenario", "/lab/workflows/ui-accessibility")],
            },
        )

    @app.get("/lab/data")
    def lab_data_page() -> str:
        return render_template(
            "lab_topic.html",
            page="lab",
            topic={
                "eyebrow": "DATABASE / DATA TESTING",
                "title": "Assert facts across the UI, API, and SQLite.",
                "lede": "The QA Lab catalog and orders are synthetic, per-account records that make database checks safe to repeat.",
                "checks": ["Inspect unique keys: one SKU per user and one order per user/idempotency key.", "Verify foreign keys: order lines reference catalog products owned by the same signed-in user.", "Create negative cases: duplicate line, invalid quantity, unknown product, and insufficient stock.", "Compare a saved QA run with its linked API/data evidence before marking it passed."],
                "links": [("Try the practice API", "/lab/api"), ("Open data scenario", "/lab/workflows/data-integrity")],
            },
        )

    @app.get("/lab/accessibility")
    def lab_accessibility_page() -> str:
        return render_template(
            "lab_topic.html",
            page="lab",
            topic={
                "eyebrow": "ACCESSIBILITY / COMPATIBILITY",
                "title": "Make each workflow usable before automating it.",
                "lede": "Use keyboard-only navigation, visible focus, labels, headings, live announcements, and reduced motion to create meaningful checks.",
                "checks": ["Tab through sign-in, profile, jobs, and applications without a mouse.", "Check that each input has a label and each action uses a semantic button or link.", "Run an axe scan in a Playwright test across dashboard, jobs, and builder.", "Repeat smoke flows in Chromium, Firefox, WebKit, and 360px/768px/1440px viewports."],
                "links": [("Open UI scenario", "/lab/workflows/ui-accessibility"), ("Open compatibility scenario", "/lab/workflows/compatibility-responsive")],
            },
        )

    @app.get("/lab/quality")
    def lab_quality_page() -> str:
        return render_template(
            "lab_topic.html",
            page="lab",
            topic={
                "eyebrow": "SECURITY / PERFORMANCE / CI",
                "title": "Turn tests into a safe release gate.",
                "lede": "Measure deterministic local endpoints and validate authorization boundaries; do not scan or load-test third-party job sources.",
                "checks": ["Check unauthenticated requests return 401 and mutations without CSRF return 403.", "Use two accounts to verify a job, resume, order, or test run cannot be read by ID.", "Measure dashboard, catalog, and job-list response times using local fixture data.", "Run compilation, Python tests, JavaScript syntax, contracts, browser smoke, and Docker build in CI."],
                "links": [("Open security scenario", "/lab/workflows/security-session"), ("Open performance scenario", "/lab/workflows/performance-resilience"), ("Open CI scenario", "/lab/workflows/cicd-release-gate")],
            },
        )

    @app.get("/lab/runs")
    def lab_runs_page() -> str:
        return render_template("lab_runs.html", page="lab-runs", scenarios=public_scenarios())

    @app.get("/openapi.json")
    def lab_openapi() -> Any:
        return jsonify(openapi_document(request.host_url.rstrip("/")))

    @app.get("/api/lab/scenarios")
    def lab_scenarios_api() -> Any:
        return jsonify({"scenarios": public_scenarios()})

    @app.get("/api/lab/scenarios/<slug>")
    def lab_scenario_api(slug: str) -> Any:
        scenario = scenario_by_slug(slug)
        if not scenario:
            return jsonify({"error": "QA Lab scenario not found."}), 404
        return jsonify({"scenario": scenario})

    @app.get("/api/lab/catalog")
    def lab_catalog_api() -> Any:
        user_id = current_owner_id()
        ensure_lab_catalog(app, user_id)
        category = plain_text(request.args.get("category"), 80)
        try:
            page = max(1, int(request.args.get("page", "1")))
            page_size = min(25, max(1, int(request.args.get("page_size", "10"))))
        except ValueError:
            return jsonify({"error": "page and page_size must be whole numbers."}), 400
        clauses = ["user_id = ?", "active = 1"]
        values: list[Any] = [user_id]
        if category:
            clauses.append("category = ?")
            values.append(category)
        where = " WHERE " + " AND ".join(clauses)
        with closing(get_db(app)) as conn:
            total = conn.execute("SELECT COUNT(*) AS count FROM lab_catalog_items" + where, values).fetchone()["count"]
            rows = conn.execute(
                "SELECT * FROM lab_catalog_items" + where + " ORDER BY sku ASC LIMIT ? OFFSET ?",
                (*values, page_size, (page - 1) * page_size),
            ).fetchall()
        return jsonify({"items": [lab_catalog_item_to_dict(row) for row in rows], "meta": {"page": page, "page_size": page_size, "total": total}})

    @app.get("/api/lab/orders")
    def lab_orders_api() -> Any:
        user_id = current_owner_id()
        ensure_lab_catalog(app, user_id)
        with closing(get_db(app)) as conn:
            ids = conn.execute("SELECT id FROM lab_orders WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 50", (user_id,)).fetchall()
            orders = [lab_order_to_dict(conn, int(row["id"]), user_id) for row in ids]
        return jsonify({"orders": [item for item in orders if item]})

    @app.post("/api/lab/orders")
    def create_lab_order() -> Any:
        user_id = current_owner_id()
        ensure_lab_catalog(app, user_id)
        try:
            customer_name, items, idempotency_key = normalise_order_payload(request_json_object())
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        now = utc_now()
        with closing(get_db(app)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id FROM lab_orders WHERE user_id = ? AND idempotency_key = ?", (user_id, idempotency_key)
            ).fetchone()
            if existing:
                order = lab_order_to_dict(conn, int(existing["id"]), user_id)
                conn.commit()
                return jsonify({"order": order, "idempotent": True, "message": "Existing order returned for this idempotency key."})
            product_ids = [item["product_id"] for item in items]
            placeholders = ",".join("?" for _ in product_ids)
            products = conn.execute(
                f"SELECT * FROM lab_catalog_items WHERE user_id = ? AND active = 1 AND id IN ({placeholders})",
                (user_id, *product_ids),
            ).fetchall()
            products_by_id = {int(row["id"]): row for row in products}
            if len(products_by_id) != len(items):
                conn.rollback()
                return jsonify({"error": "One or more product_id values are unavailable in your QA Lab catalog."}), 400
            for item in items:
                product = products_by_id[item["product_id"]]
                if item["quantity"] > int(product["stock"]):
                    conn.rollback()
                    return jsonify({"error": f"Insufficient stock for {product['sku']}. Available: {product['stock']}."}), 409
            total_paise = sum(int(products_by_id[item["product_id"]]["price_paise"]) * item["quantity"] for item in items)
            order_number = f"LAB-{user_id}-{uuid4().hex[:10].upper()}"
            cursor = conn.execute(
                """
                INSERT INTO lab_orders (user_id, order_number, customer_name, status, total_paise, idempotency_key, created_at, updated_at)
                VALUES (?, ?, ?, 'created', ?, ?, ?, ?)
                """,
                (user_id, order_number, customer_name, total_paise, idempotency_key, now, now),
            )
            order_id = int(cursor.lastrowid)
            for item in items:
                product = products_by_id[item["product_id"]]
                conn.execute(
                    "INSERT INTO lab_order_items (order_id, product_id, quantity, unit_price_paise) VALUES (?, ?, ?, ?)",
                    (order_id, item["product_id"], item["quantity"], int(product["price_paise"])),
                )
                conn.execute(
                    "UPDATE lab_catalog_items SET stock = stock - ?, updated_at = ? WHERE id = ? AND user_id = ?",
                    (item["quantity"], now, item["product_id"], user_id),
                )
            order = lab_order_to_dict(conn, order_id, user_id)
            conn.commit()
        return jsonify({"order": order, "idempotent": False, "message": "Synthetic QA Lab order created."}), 201

    @app.get("/api/lab/runs")
    def lab_runs_api() -> Any:
        user_id = current_owner_id()
        with closing(get_db(app)) as conn:
            rows = conn.execute("SELECT * FROM qa_runs WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 60", (user_id,)).fetchall()
        runs = []
        for row in rows:
            item = {key: row[key] for key in row.keys()}
            item["summary"] = json_value(item.pop("summary_json", "{}"), {})
            runs.append(item)
        return jsonify({"runs": runs})

    @app.post("/api/lab/runs")
    def create_lab_run() -> Any:
        user_id = current_owner_id()
        data = request_json_object()
        scenario_slug = plain_text(data.get("scenario_slug"), 100)
        if not scenario_by_slug(scenario_slug):
            return jsonify({"error": "Choose a QA Lab scenario."}), 400
        suite = plain_text(data.get("suite"), 60).casefold()
        if suite not in {"smoke", "sanity", "regression", "integration", "api", "data", "ui", "accessibility", "compatibility", "performance", "security", "cicd"}:
            return jsonify({"error": "Choose a supported test suite."}), 400
        status = plain_text(data.get("status"), 30).casefold()
        if status not in {"passed", "failed", "blocked", "in_progress"}:
            return jsonify({"error": "Choose passed, failed, blocked, or in_progress."}), 400
        now = utc_now()
        notes = plain_text(data.get("notes"), 4000)
        summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
        with closing(get_db(app)) as conn:
            cursor = conn.execute(
                """
                INSERT INTO qa_runs (user_id, scenario_slug, suite, status, notes, summary_json, started_at, completed_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, scenario_slug, suite, status, notes, json.dumps(summary, ensure_ascii=False), now, now if status != "in_progress" else None, now),
            )
            conn.commit()
        return jsonify({"id": int(cursor.lastrowid), "message": "QA run saved."}), 201

    @app.get("/api/lab/quality-summary")
    def lab_quality_summary() -> Any:
        user_id = current_owner_id()
        ensure_lab_catalog(app, user_id)
        with closing(get_db(app)) as conn:
            run_rows = conn.execute("SELECT status, COUNT(*) AS count FROM qa_runs WHERE user_id = ? GROUP BY status", (user_id,)).fetchall()
            catalog_count = conn.execute("SELECT COUNT(*) AS count FROM lab_catalog_items WHERE user_id = ?", (user_id,)).fetchone()["count"]
        counts = {row["status"]: row["count"] for row in run_rows}
        return jsonify(
            {
                "scenarios": len(LAB_SCENARIOS),
                "catalog_items": catalog_count,
                "runs": counts,
                "gates": ["Authenticated ownership", "CSRF on mutations", "Input validation", "Deterministic test data", "No third-party load testing"],
            }
        )

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
    def resumes_page() -> Any:
        return render_template("resumes.html", page="resumes")

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
        user_id = current_owner_id()
        message = plain_text(data.get("message"), 5000)
        if not message:
            return jsonify({"error": "Write a request for the local workspace assistant."}), 400
        selected_model = get_setting(app, "local_ai_model") or DEFAULT_MODEL
        task_id = uuid4().hex
        with CHAT_TASKS_LOCK:
            CHAT_TASKS[task_id] = {"owner_id": user_id, "state": "queued", "stage": "queued", "detail": "Request queued locally."}
        CHAT_TASK_EXECUTOR.submit(run_chat_task, task_id, message, selected_model, read_profile(app))
        return jsonify({"task_id": task_id}), 202

    @app.get("/api/workspace-chat/status/<task_id>")
    def workspace_chat_status(task_id: str) -> Any:
        with CHAT_TASKS_LOCK:
            task = dict(CHAT_TASKS.get(task_id) or {})
        if not task or int(task.get("owner_id") or 0) != current_owner_id():
            return jsonify({"error": "Assistant request not found or expired."}), 404
        task.pop("owner_id", None)
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
        if not app.config["LOCAL_CODE_ASSISTANT"] or getattr(current_user, "role", "user") != "admin":
            return jsonify({"error": "Source edits are available only to the local administrator workspace."}), 403
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
        user_id = current_owner_id()
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
                WHERE id = ? AND user_id = ?
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
                    user_id,
                ),
            )
            conn.commit()
            updated = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
        return jsonify({"job": job_to_dict(updated, read_profile(app)), "updated": True})

    @app.get("/api/jobs")
    def list_jobs() -> Any:
        user_id = current_owner_id()
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
        clauses: list[str] = ["user_id = ?"]
        values: list[Any] = [user_id]
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

    @app.get("/api/job-search-runs/<int:search_run_id>")
    def get_job_search_run(search_run_id: int) -> Any:
        run = search_run_summary(app, search_run_id)
        if not run:
            return jsonify({"error": "Search run not found."}), 404
        return jsonify({"search_run": run, "results": search_run_results(app, search_run_id)})

    @app.get("/api/job-search-runs/<int:search_run_id>/results")
    def get_job_search_run_results(search_run_id: int) -> Any:
        if not search_run_summary(app, search_run_id):
            return jsonify({"error": "Search run not found."}), 404
        return jsonify({"results": search_run_results(app, search_run_id)})

    @app.get("/api/jobs/<int:job_id>")
    def get_job(job_id: int) -> Any:
        return jsonify({"job": job_to_dict(query_job(app, job_id), read_profile(app))})

    @app.post("/api/jobs/<int:job_id>/decision")
    def decide_job(job_id: int) -> Any:
        data = request_json_object()
        user_id = current_owner_id()
        status = str(data.get("status", "")).lower()
        if status not in {"approved", "rejected", "applied", "interview", "offer", "closed"}:
            return jsonify({"error": "Choose approved, rejected, applied, interview, offer, or closed."}), 400
        now = utc_now()
        with closing(get_db(app)) as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
            if not job:
                return jsonify({"error": "Job not found."}), 404
            conn.execute(
                "UPDATE jobs SET status = ?, closed_at = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (status, now if status == "closed" else None, now, job_id, user_id),
            )
            if status in {"rejected", "closed"}:
                # A removed opportunity should not remain in the active
                # application pipeline.
                conn.execute("DELETE FROM applications WHERE job_id = ? AND user_id = ?", (job_id, user_id))
            else:
                conn.execute(
                    """
                    INSERT INTO applications (user_id, job_id, status, notes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at
                    """,
                    (user_id, job_id, status, plain_text(data.get("notes"), 2000), now, now),
                )
            conn.commit()
            updated = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
        return jsonify({"job": job_to_dict(updated, read_profile(app)), "message": f"Marked {status}."})

    @app.post("/api/jobs/<int:job_id>/close")
    def close_job(job_id: int) -> Any:
        user_id = current_owner_id()
        now = utc_now()
        with closing(get_db(app)) as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
            if not job:
                return jsonify({"error": "Job not found."}), 404
            conn.execute("UPDATE jobs SET status = 'closed', closed_at = ?, updated_at = ? WHERE id = ? AND user_id = ?", (now, now, job_id, user_id))
            conn.execute("DELETE FROM applications WHERE job_id = ? AND user_id = ?", (job_id, user_id))
            conn.commit()
            updated = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
        return jsonify({"job": job_to_dict(updated, read_profile(app)), "message": "Opportunity closed and removed from the application queue."})

    @app.post("/api/jobs/<int:job_id>/reopen")
    def reopen_job(job_id: int) -> Any:
        user_id = current_owner_id()
        now = utc_now()
        with closing(get_db(app)) as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
            if not job:
                return jsonify({"error": "Job not found."}), 404
            conn.execute("UPDATE jobs SET status = 'new', closed_at = NULL, updated_at = ? WHERE id = ? AND user_id = ?", (now, job_id, user_id))
            conn.commit()
            updated = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
        return jsonify({"job": job_to_dict(updated, read_profile(app)), "message": "Opportunity restored to the new-review queue."})

    @app.post("/api/jobs/discover")
    def discover_jobs() -> Any:
        user_id = current_owner_id()
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
                    cached_run_id = int(cached.get("search_run_id") or 0)
                    cached_run = search_run_summary(app, cached_run_id) if cached_run_id else None
                    results = search_run_results(app, cached_run_id) if cached_run else []
                    return jsonify(
                        {
                            "added": int(cached_run.get("created_count") or 0) if cached_run else 0,
                            "existing": int(cached_run.get("existing_count") or 0) if cached_run else 0,
                            "reviewed": int(cached.get("result_count") or 0),
                            "search_run_id": cached_run_id or None,
                            "job_ids": [item["id"] for item in results],
                            "results": results,
                            # Compatibility for existing clients; new UI uses
                            # `results` because these are persisted run records.
                            "jobs": results,
                            "checked_at": cached["checked_at"],
                            "source_report": cached.get("source_report") or [],
                            "cached": True,
                            "message": f"Showing the saved {market} search from {cached['checked_at']}. Its exact results are shown below.",
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
                cached_run_id = int(cached.get("search_run_id") or 0)
                cached_run = search_run_summary(app, cached_run_id) if cached_run_id else None
                results = search_run_results(app, cached_run_id) if cached_run else []
                return jsonify(
                    {
                        "added": int(cached_run.get("created_count") or 0) if cached_run else 0,
                        "existing": int(cached_run.get("existing_count") or 0) if cached_run else 0,
                        "reviewed": int(cached.get("result_count") or 0),
                        "search_run_id": cached_run_id or None,
                        "job_ids": [item["id"] for item in results],
                        "results": results,
                        "jobs": results,
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
        results: list[dict[str, Any]] = []
        for candidate in candidates:
            try:
                job, created = create_job(app, candidate, source="Remotive")
                added += int(created)
                reviewed += 1
                job["search_result_state"] = "created" if created else "existing"
                results.append(job)
            except ValueError:
                continue
        checked_at = utc_now()
        available = sum(1 for item in source_report if item.get("status") == "ok")
        source_errors = [str(item.get("detail") or "") for item in source_report if item.get("status") == "unavailable"]
        with closing(get_db(app)) as conn:
            cursor = conn.execute(
                """
                INSERT INTO job_search_runs
                (user_id, query, market, role_track, filters_json, source_report, reviewed_count, created_count,
                 existing_count, status, checked_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete', ?, ?)
                """,
                (
                    user_id,
                    query,
                    market,
                    role_track or "All QA tracks",
                    json.dumps({"product_only": product_only, "salary_only": salary_only}, ensure_ascii=False),
                    json.dumps(source_report, ensure_ascii=False),
                    reviewed,
                    added,
                    max(0, reviewed - added),
                    checked_at,
                    checked_at,
                ),
            )
            search_run_id = int(cursor.lastrowid)
            for rank, job in enumerate(results, start=1):
                conn.execute(
                    """
                    INSERT INTO job_search_results (search_run_id, job_id, rank, result_state)
                    VALUES (?, ?, ?, ?)
                    """,
                    (search_run_id, job["id"], rank, job.get("search_result_state", "existing")),
                )
            conn.commit()
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
            search_run_id,
        )
        # Fetch from the persisted run, rather than returning raw provider
        # records. This guarantees the UI has the same IDs/statuses the inbox
        # uses and makes duplicate/approved results visible instead of hidden.
        persisted_results = search_run_results(app, search_run_id)
        return jsonify(
            {
                "added": added,
                "existing": max(0, reviewed - added),
                "reviewed": reviewed,
                "search_run_id": search_run_id,
                "job_ids": [item["id"] for item in persisted_results],
                "results": persisted_results,
                "jobs": persisted_results,
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
        user_id = current_owner_id()
        with closing(get_db(app)) as conn:
            rows = conn.execute(
                """
                SELECT a.id, a.job_id, a.status, a.notes, a.application_kind,
                       a.contact_name, a.contact_role, a.contact_email, a.contact_phone,
                       a.referral_name, a.referral_contact, a.next_step, a.created_at, a.updated_at,
                       j.title, j.company, j.location, j.source_url, j.source
                FROM applications a JOIN jobs j ON j.id = a.job_id
                WHERE a.user_id = ? AND j.user_id = ?
                ORDER BY a.updated_at DESC, a.id DESC
                """
                ,
                (user_id, user_id),
            ).fetchall()
        return jsonify({"applications": [{key: row[key] for key in row.keys()} for row in rows]})

    @app.post("/api/applications")
    def add_manual_application() -> Any:
        data = request_json_object()
        user_id = current_owner_id()
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
            conn.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?", (status, now, job["id"], user_id))
            cursor = conn.execute(
                """
                INSERT INTO applications
                (user_id, job_id, status, notes, application_kind, contact_name, contact_role, contact_email, contact_phone,
                 referral_name, referral_contact, next_step, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
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
                FROM applications a JOIN jobs j ON j.id = a.job_id
                WHERE a.id = ? AND a.user_id = ? AND j.user_id = ?
                """,
                (cursor.lastrowid, user_id, user_id),
            ).fetchone()
        return jsonify({"application": {key: row[key] for key in row.keys()}, "message": "Manual opportunity added to your application pipeline."}), 201

    @app.patch("/api/applications/<int:application_id>")
    def update_application(application_id: int) -> Any:
        data = request_json_object()
        user_id = current_owner_id()
        status = str(data.get("status", "")).lower()
        if status not in APPLICATION_STATUSES:
            return jsonify({"error": "Unsupported application status."}), 400
        now = utc_now()
        with closing(get_db(app)) as conn:
            app_row = conn.execute("SELECT * FROM applications WHERE id = ? AND user_id = ?", (application_id, user_id)).fetchone()
            if not app_row:
                return jsonify({"error": "Application not found."}), 404
            def update_value(key: str, limit: int) -> str:
                return plain_text(data.get(key), limit) if key in data else str(app_row[key] or "")

            notes = update_value("notes", 2000)
            conn.execute(
                """
                UPDATE applications SET status = ?, notes = ?, contact_name = ?, contact_role = ?, contact_email = ?,
                    contact_phone = ?, referral_name = ?, referral_contact = ?, next_step = ?, updated_at = ? WHERE id = ? AND user_id = ?
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
                    user_id,
                ),
            )
            conn.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?", (status, now, app_row["job_id"], user_id))
            conn.commit()
        return jsonify({"message": "Application updated."})

    @app.get("/api/resumes")
    def list_resume_versions() -> Any:
        user_id = current_owner_id()
        with closing(get_db(app)) as conn:
            rows = conn.execute(
                """
                SELECT rv.id, rv.job_id, rv.title, rv.filename, rv.created_at,
                       j.company, j.status AS job_status
                FROM resume_versions rv LEFT JOIN jobs j ON j.id = rv.job_id AND j.user_id = rv.user_id
                WHERE rv.user_id = ?
                ORDER BY rv.created_at DESC, rv.id DESC LIMIT 30
                """
                ,
                (user_id,),
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
        user_id = current_owner_id()
        tailored = build_tailored_resume(profile, analysis, title)
        filename = resume_filename(profile, title)
        document = build_resume_document(tailored, title, company, layout)
        now = utc_now()
        with closing(get_db(app)) as conn:
            cursor = conn.execute(
                """
                INSERT INTO resume_versions (user_id, job_id, title, filename, profile_snapshot, tailored_snapshot, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
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
        user_id = current_owner_id()
        with closing(get_db(app)) as conn:
            row = conn.execute("SELECT * FROM resume_versions WHERE id = ? AND user_id = ?", (version_id, user_id)).fetchone()
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
        user_id = current_owner_id()
        profile = read_profile(app)
        with closing(get_db(app)) as conn:
            status_rows = conn.execute("SELECT status, COUNT(*) AS count FROM jobs WHERE user_id = ? GROUP BY status", (user_id,)).fetchall()
            latest_rows = conn.execute("SELECT * FROM jobs WHERE user_id = ? AND status != 'closed' ORDER BY updated_at DESC, id DESC LIMIT 4", (user_id,)).fetchall()
            version_count = conn.execute("SELECT COUNT(*) AS count FROM resume_versions WHERE user_id = ?", (user_id,)).fetchone()["count"]
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
