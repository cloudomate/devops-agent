"""SQLite database — projects, environments, deployments."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import os
DB_PATH = Path(os.getenv("DATA_DIR", ".")) / "devops_agent.db"

# Reserved project name for global (fallback) environments
GLOBAL_PROJECT_NAME = "__global__"


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT UNIQUE NOT NULL,
                github_repo  TEXT,
                description  TEXT,
                github_token TEXT,
                created_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS environments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name        TEXT NOT NULL,
                type        TEXT NOT NULL,       -- 'kubernetes' | 'ssh'
                config      TEXT NOT NULL,       -- JSON of type-specific fields
                health_check_url TEXT,
                current_image    TEXT,
                previous_image   TEXT,
                updated_at       TEXT,
                UNIQUE(project_id, name)
            );

            CREATE TABLE IF NOT EXISTS deployments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id   INTEGER NOT NULL REFERENCES projects(id),
                environment  TEXT NOT NULL,
                image_or_ref TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending',
                reason       TEXT,
                triggered_by TEXT,
                started_at   TEXT NOT NULL,
                finished_at  TEXT,
                output       TEXT
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                entra_oid     TEXT UNIQUE,
                username      TEXT NOT NULL,
                display_name  TEXT,
                role          TEXT NOT NULL DEFAULT 'developer',
                created_at    TEXT NOT NULL,
                updated_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS user_sessions (
                token      TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS project_members (
                project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                PRIMARY KEY (project_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS deployment_requests (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                environment     TEXT NOT NULL,
                image_or_ref    TEXT NOT NULL,
                plan_markdown   TEXT NOT NULL,
                plan_config     TEXT NOT NULL,   -- JSON
                status          TEXT NOT NULL DEFAULT 'pending_review',
                requested_by    INTEGER REFERENCES users(id),
                reviewed_by     INTEGER REFERENCES users(id),
                session_id      TEXT NOT NULL,
                reject_reason   TEXT,
                deployment_id   INTEGER REFERENCES deployments(id),
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS system_settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        # Live migrations
        cols = {row[1] for row in con.execute("PRAGMA table_info(projects)").fetchall()}
        if "github_token" not in cols:
            con.execute("ALTER TABLE projects ADD COLUMN github_token TEXT")
        if "last_commit_sha" not in cols:
            con.execute("ALTER TABLE projects ADD COLUMN last_commit_sha TEXT")

    # Ensure the global sentinel project always exists
    get_or_create_global_project()


# ─── Global project ───────────────────────────────────────────────────────────

def get_or_create_global_project() -> dict[str, Any]:
    """Return the __global__ sentinel project, creating it if absent."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM projects WHERE name=?", (GLOBAL_PROJECT_NAME,)
        ).fetchone()
        if row:
            return dict(row)
        cur = con.execute(
            "INSERT INTO projects (name, github_repo, description, created_at) VALUES (?,?,?,?)",
            (GLOBAL_PROJECT_NAME, "", "Global fallback environments", datetime.utcnow().isoformat()),
        )
        row = con.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


# ─── Projects ────────────────────────────────────────────────────────────────

def create_project(
    name: str,
    github_repo: str = "",
    description: str = "",
    github_token: str = "",
) -> dict[str, Any]:
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO projects (name, github_repo, description, github_token, created_at) VALUES (?,?,?,?,?)",
            (name, github_repo, description, github_token or None, datetime.utcnow().isoformat()),
        )
        return get_project_by_id(cur.lastrowid)  # type: ignore[arg-type]


def get_project_by_id(project_id: int) -> dict[str, Any]:
    with _conn() as con:
        row = con.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return dict(row) if row else {}


def get_project_by_name(name: str) -> dict[str, Any] | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None


def list_projects() -> list[dict[str, Any]]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM projects WHERE name != ? ORDER BY name", (GLOBAL_PROJECT_NAME,)
        ).fetchall()
        return [dict(r) for r in rows]


def update_project(project_id: int, **kwargs: Any) -> None:
    allowed = {"name", "github_repo", "description", "github_token", "last_commit_sha"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with _conn() as con:
        con.execute(f"UPDATE projects SET {sets} WHERE id=?", (*fields.values(), project_id))


def delete_project(project_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM projects WHERE id=?", (project_id,))


# ─── Environments ─────────────────────────────────────────────────────────────

def upsert_environment(
    project_id: int,
    name: str,
    type_: str,
    config: dict[str, Any],
    health_check_url: str | None = None,
) -> dict[str, Any]:
    with _conn() as con:
        con.execute(
            """INSERT INTO environments (project_id, name, type, config, health_check_url, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(project_id, name) DO UPDATE SET
                   type=excluded.type,
                   config=excluded.config,
                   health_check_url=excluded.health_check_url,
                   updated_at=excluded.updated_at""",
            (project_id, name, type_, json.dumps(config), health_check_url, datetime.utcnow().isoformat()),
        )
    return get_environment(project_id, name)  # type: ignore[return-value]


def get_environment(project_id: int, name: str) -> dict[str, Any] | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM environments WHERE project_id=? AND name=?", (project_id, name)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["config"] = json.loads(d["config"])
        return d


def list_environments(project_id: int) -> list[dict[str, Any]]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM environments WHERE project_id=? ORDER BY name", (project_id,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["config"] = json.loads(d["config"])
            result.append(d)
        return result


def list_environments_with_global(project_id: int) -> list[dict[str, Any]]:
    """Return project-specific environments, falling back to global for missing names."""
    project_envs = {e["name"]: e for e in list_environments(project_id)}

    global_proj = get_or_create_global_project()
    global_envs = list_environments(global_proj["id"])

    result = list(project_envs.values())
    project_env_names = set(project_envs)
    for g in global_envs:
        if g["name"] not in project_env_names:
            g = dict(g)
            g["is_global"] = True
            result.append(g)

    result.sort(key=lambda e: e["name"])
    return result


def list_global_environments() -> list[dict[str, Any]]:
    global_proj = get_or_create_global_project()
    envs = list_environments(global_proj["id"])
    for e in envs:
        e["is_global"] = True
    return envs


def delete_environment(project_id: int, name: str) -> None:
    with _conn() as con:
        con.execute(
            "DELETE FROM environments WHERE project_id=? AND name=?", (project_id, name)
        )


def update_environment_state(project_id: int, env_name: str, image_or_ref: str) -> None:
    with _conn() as con:
        con.execute(
            """UPDATE environments
               SET previous_image=current_image, current_image=?, updated_at=?
               WHERE project_id=? AND name=?""",
            (image_or_ref, datetime.utcnow().isoformat(), project_id, env_name),
        )


# ─── Deployments ─────────────────────────────────────────────────────────────

def start_deployment(
    project_id: int,
    environment: str,
    image_or_ref: str,
    reason: str | None = None,
    triggered_by: str | None = None,
) -> int:
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO deployments
               (project_id, environment, image_or_ref, status, reason, triggered_by, started_at)
               VALUES (?,?,?,'running',?,?,?)""",
            (project_id, environment, image_or_ref, reason, triggered_by, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid  # type: ignore[return-value]


def finish_deployment(deploy_id: int, status: str, output: str | None = None) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE deployments SET status=?, finished_at=?, output=? WHERE id=?",
            (status, datetime.utcnow().isoformat(), output, deploy_id),
        )


def list_deployments(project_id: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    with _conn() as con:
        if project_id is not None:
            rows = con.execute(
                """SELECT d.*, p.name as project_name FROM deployments d
                   JOIN projects p ON p.id=d.project_id
                   WHERE d.project_id=? ORDER BY d.id DESC LIMIT ?""",
                (project_id, limit),
            ).fetchall()
        else:
            rows = con.execute(
                """SELECT d.*, p.name as project_name FROM deployments d
                   JOIN projects p ON p.id=d.project_id
                   ORDER BY d.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


# ─── Chat history ─────────────────────────────────────────────────────────────

def save_message(session_id: str, role: str, content: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
            (session_id, role, content, datetime.utcnow().isoformat()),
        )


def get_session_history(session_id: str, limit: int = 50) -> list[dict[str, str]]:
    with _conn() as con:
        rows = con.execute(
            """SELECT role, content FROM chat_messages
               WHERE session_id=? ORDER BY id DESC LIMIT ?""",
            (session_id, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# ─── System settings ──────────────────────────────────────────────────────────

def get_all_system_settings() -> dict[str, str]:
    with _conn() as con:
        rows = con.execute("SELECT key, value FROM system_settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


def set_system_settings(pairs: dict[str, str]) -> None:
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        for key, value in pairs.items():
            con.execute(
                """INSERT INTO system_settings (key, value, updated_at) VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, now),
            )


def delete_system_setting(key: str) -> None:
    with _conn() as con:
        con.execute("DELETE FROM system_settings WHERE key=?", (key,))


# ─── Users ────────────────────────────────────────────────────────────────────

def create_or_update_user(entra_oid: str, username: str, display_name: str, role: str) -> dict[str, Any]:
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        con.execute(
            """INSERT INTO users (entra_oid, username, display_name, role, created_at, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(entra_oid) DO UPDATE SET
                   username=excluded.username,
                   display_name=excluded.display_name,
                   role=excluded.role,
                   updated_at=excluded.updated_at""",
            (entra_oid, username, display_name, role, now, now),
        )
        row = con.execute("SELECT * FROM users WHERE entra_oid=?", (entra_oid,)).fetchone()
        return dict(row)


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def list_users() -> list[dict[str, Any]]:
    with _conn() as con:
        rows = con.execute("SELECT id, username, display_name, role, created_at FROM users ORDER BY username").fetchall()
        return [dict(r) for r in rows]


def update_user_role(user_id: int, role: str) -> None:
    with _conn() as con:
        con.execute("UPDATE users SET role=?, updated_at=? WHERE id=?",
                    (role, datetime.utcnow().isoformat(), user_id))


# ─── Sessions ─────────────────────────────────────────────────────────────────

def create_session(token: str, user_id: int, expires_at: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO user_sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (token, user_id, datetime.utcnow().isoformat(), expires_at),
        )


def get_session(token: str) -> dict[str, Any] | None:
    """Returns merged user+session dict, or None if expired/not found."""
    with _conn() as con:
        row = con.execute(
            """SELECT u.id, u.username, u.display_name, u.role, s.expires_at
               FROM user_sessions s JOIN users u ON u.id=s.user_id
               WHERE s.token=?""",
            (token,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        if d["expires_at"] < datetime.utcnow().isoformat():
            con.execute("DELETE FROM user_sessions WHERE token=?", (token,))
            return None
        return d


def delete_session(token: str) -> None:
    with _conn() as con:
        con.execute("DELETE FROM user_sessions WHERE token=?", (token,))


# ─── Project membership ───────────────────────────────────────────────────────

def add_project_member(project_id: int, user_id: int) -> None:
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO project_members (project_id, user_id) VALUES (?,?)",
            (project_id, user_id),
        )


def remove_project_member(project_id: int, user_id: int) -> None:
    with _conn() as con:
        con.execute(
            "DELETE FROM project_members WHERE project_id=? AND user_id=?",
            (project_id, user_id),
        )


def list_project_members(project_id: int) -> list[dict[str, Any]]:
    with _conn() as con:
        rows = con.execute(
            """SELECT u.id, u.username, u.display_name, u.role
               FROM project_members pm JOIN users u ON u.id=pm.user_id
               WHERE pm.project_id=?""",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def is_project_member(project_id: int, user_id: int) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM project_members WHERE project_id=? AND user_id=?",
            (project_id, user_id),
        ).fetchone()
        return row is not None


# ─── Deployment requests ──────────────────────────────────────────────────────

def create_deployment_request(
    project_id: int,
    environment: str,
    image_or_ref: str,
    plan_markdown: str,
    plan_config: dict[str, Any],
    requested_by: int | None,
    session_id: str,
) -> int:
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        # Supersede any existing pending_review requests for the same project+env
        con.execute(
            """UPDATE deployment_requests SET status='superseded', updated_at=?
               WHERE project_id=? AND environment=? AND status='pending_review'""",
            (now, project_id, environment),
        )
        cur = con.execute(
            """INSERT INTO deployment_requests
               (project_id, environment, image_or_ref, plan_markdown, plan_config,
                status, requested_by, session_id, created_at, updated_at)
               VALUES (?,?,?,?,?,'pending_review',?,?,?,?)""",
            (project_id, environment, image_or_ref, plan_markdown,
             json.dumps(plan_config), requested_by, session_id, now, now),
        )
        return cur.lastrowid  # type: ignore[return-value]


def get_deployment_request(request_id: int) -> dict[str, Any] | None:
    with _conn() as con:
        row = con.execute(
            """SELECT dr.*, p.name as project_name,
                      u1.display_name as requester_name, u1.username as requester_username,
                      u2.display_name as reviewer_name
               FROM deployment_requests dr
               JOIN projects p ON p.id=dr.project_id
               LEFT JOIN users u1 ON u1.id=dr.requested_by
               LEFT JOIN users u2 ON u2.id=dr.reviewed_by
               WHERE dr.id=?""",
            (request_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["plan_config"] = json.loads(d["plan_config"])
        return d


def list_deployment_requests(
    status: str | None = None,
    project_id: int | None = None,
    requested_by: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    conditions = ["dr.status != 'superseded'"]
    params: list[Any] = []
    if status:
        conditions.append("dr.status=?")
        params.append(status)
    if project_id is not None:
        conditions.append("dr.project_id=?")
        params.append(project_id)
    if requested_by is not None:
        conditions.append("dr.requested_by=?")
        params.append(requested_by)
    where = "WHERE " + " AND ".join(conditions)
    params.append(limit)
    with _conn() as con:
        rows = con.execute(
            f"""SELECT dr.*, p.name as project_name,
                       u1.display_name as requester_name, u1.username as requester_username
                FROM deployment_requests dr
                JOIN projects p ON p.id=dr.project_id
                LEFT JOIN users u1 ON u1.id=dr.requested_by
                {where}
                ORDER BY dr.id DESC LIMIT ?""",
            params,
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["plan_config"] = json.loads(d["plan_config"])
            result.append(d)
        return result


def update_deployment_request(request_id: int, **kwargs: Any) -> None:
    allowed = {"image_or_ref", "plan_markdown", "plan_config", "status",
               "reviewed_by", "reject_reason", "deployment_id"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    if "plan_config" in fields and isinstance(fields["plan_config"], dict):
        fields["plan_config"] = json.dumps(fields["plan_config"])
    fields["updated_at"] = datetime.utcnow().isoformat()
    sets = ", ".join(f"{k}=?" for k in fields)
    with _conn() as con:
        con.execute(
            f"UPDATE deployment_requests SET {sets} WHERE id=?",
            (*fields.values(), request_id),
        )


def count_deployment_requests(status: str) -> int:
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM deployment_requests WHERE status=?", (status,)
        ).fetchone()
        return row[0] if row else 0


def list_projects_for_user(user_id: int, role: str) -> list[dict[str, Any]]:
    """Admins/DevOps see all projects; developers only see their assigned projects."""
    status_subq = """
        LEFT JOIN (
            SELECT project_id,
                   status AS latest_request_status,
                   environment AS latest_request_env,
                   id AS latest_request_id,
                   created_at AS latest_request_at
            FROM deployment_requests dr1
            WHERE id = (SELECT id FROM deployment_requests dr2
                        WHERE dr2.project_id = dr1.project_id
                        ORDER BY created_at DESC LIMIT 1)
        ) dr ON dr.project_id = p.id
    """
    with _conn() as con:
        if role in ("admin", "devops"):
            rows = con.execute(
                f"SELECT p.*, dr.latest_request_status, dr.latest_request_env, dr.latest_request_id, dr.latest_request_at FROM projects p {status_subq} WHERE p.name != ? ORDER BY p.name",
                (GLOBAL_PROJECT_NAME,),
            ).fetchall()
        else:
            rows = con.execute(
                f"""SELECT p.*, dr.latest_request_status, dr.latest_request_env, dr.latest_request_id, dr.latest_request_at
                   FROM projects p
                   JOIN project_members pm ON pm.project_id=p.id
                   {status_subq}
                   WHERE pm.user_id=? AND p.name != ? ORDER BY p.name""",
                (user_id, GLOBAL_PROJECT_NAME),
            ).fetchall()
        return [dict(r) for r in rows]
