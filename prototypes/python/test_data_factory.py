"""Deterministic, tagged CareerCraft data factory for local QA exercises."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from werkzeug.security import generate_password_hash

from app import create_app, utc_now


DATASET_COUNTS = {"seed:test": 10, "seed:large": 1000, "seed:performance": 10000}


def create_dataset(database: str, run_id: str, count: int) -> dict[str, Any]:
    if count < 1 or count > 10000:
        raise ValueError("count must be between 1 and 10000")
    app = create_app({"TESTING": True, "DATABASE": database, "SECRET_KEY": "factory-only"})
    now = utc_now()
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        user_ids = []
        for index in range(count):
            email = f"qa-factory+{run_id}-{index}@example.test"
            cursor = connection.execute(
                "INSERT INTO users (email, display_name, password_hash, role, is_active, created_at, updated_at) VALUES (?, ?, ?, 'candidate', 1, ?, ?)",
                (email, f"Factory Candidate {index}", generate_password_hash("FactoryPassword123"), now, now),
            )
            user_id = cursor.lastrowid
            user_ids.append(user_id)
            role = connection.execute("SELECT id FROM roles WHERE code = 'candidate'").fetchone()
            if role:
                connection.execute("INSERT OR IGNORE INTO user_roles (user_id, role_id, created_at) VALUES (?, ?, ?)", (user_id, role[0], now))
            connection.execute("INSERT INTO user_profiles (user_id, data, updated_at) VALUES (?, ?, ?)", (user_id, '{"full_name":"Factory Candidate"}', now))
            job_cursor = connection.execute(
                "INSERT INTO jobs (user_id, external_id, source, title, company, location, description, status, created_at, updated_at) VALUES (?, ?, 'Factory', ?, ?, ?, ?, 'new', ?, ?)",
                (user_id, f"factory:{run_id}:{index}", f"QA Automation Engineer {index}", f"Factory Labs {index}", "Bengaluru, India", "Deterministic QA automation practice role with API testing, SQL, Python, and regression testing.", now, now),
            )
            connection.execute("INSERT INTO applications (user_id, job_id, status, notes, application_kind, created_at, updated_at) VALUES (?, ?, 'approved', 'Factory-generated record', 'Factory', ?, ?)", (user_id, job_cursor.lastrowid, now, now))
        connection.commit()
    return {"run_id": run_id, "users": len(user_ids), "jobs": len(user_ids), "database": str(Path(database).resolve()), "app_initialized": bool(app)}


def cleanup_dataset(database: str, run_id: str) -> int:
    if not run_id or len(run_id) > 80:
        raise ValueError("run_id is required and must be at most 80 characters")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        cursor = connection.execute("DELETE FROM users WHERE email LIKE ?", (f"qa-factory+{run_id}-%@example.test",))
        connection.commit()
        return cursor.rowcount


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or remove tagged local CareerCraft QA data.")
    parser.add_argument("command", choices=sorted(DATASET_COUNTS) + ["seed:cleanup"])
    parser.add_argument("--database", default=os.environ.get("RESUME_DB_PATH", "qa-factory.db"))
    parser.add_argument("--count", type=int)
    parser.add_argument("--run-id", default=f"run-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:6]}")
    args = parser.parse_args()
    if args.command == "seed:cleanup":
        print({"run_id": args.run_id, "deleted_users": cleanup_dataset(args.database, args.run_id)})
        return
    count = args.count or DATASET_COUNTS[args.command]
    print(create_dataset(args.database, args.run_id, count))


if __name__ == "__main__":
    main()
