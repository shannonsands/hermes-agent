"""FastAPI sidecar for Hermes Flow.

The Flow sidecar owns Flow-specific state while composing the core Hermes
dashboard and Kanban primitives. It intentionally keeps Advanced project
planning data in ``$HERMES_HOME/flow/flow.db`` and treats Kanban's ``tenant``
field as an execution projection only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from hermes_constants import get_default_hermes_root


app = FastAPI(title="Hermes Flow", version="0.1.0")

DEFAULT_DASHBOARD_URL = os.environ.get("HERMES_DASHBOARD_URL", "http://127.0.0.1:9119").rstrip("/")
DEFAULT_GATEWAY_API_URL = os.environ.get("HERMES_GATEWAY_API_URL", "http://127.0.0.1:8642").rstrip("/")
MAX_ATTACHMENT_BYTES = int(os.environ.get("HERMES_FLOW_MAX_ATTACHMENT_BYTES", str(50 * 1024 * 1024)))


def hermes_root() -> Path:
    return get_default_hermes_root().expanduser()


def flow_home() -> Path:
    path = hermes_root() / "flow"
    path.mkdir(parents=True, exist_ok=True)
    return path


def flow_db_path() -> Path:
    return flow_home() / "flow.db"


def attachments_root() -> Path:
    path = flow_home() / "attachments"
    path.mkdir(parents=True, exist_ok=True)
    return path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:14]}"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or f"project-{uuid.uuid4().hex[:6]}"


def json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, separators=(",", ":"), sort_keys=True)


def json_loads(value: Any, fallback: Any = None) -> Any:
    if value is None or value == "":
        return {} if fallback is None else fallback
    try:
        return json.loads(value)
    except Exception:
        return {} if fallback is None else fallback


def ensure_kanban_board(slug: str, *, name: str | None = None, description: str | None = None) -> dict[str, Any]:
    """Create/update the Kanban board backing a Flow project."""
    try:
        from hermes_cli import kanban_db

        return kanban_db.create_board(
            slug,
            name=name,
            description=description,
            icon="HF",
            color="#ffd343",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"kanban board setup failed: {exc}") from exc


def connect() -> sqlite3.Connection:
    path = flow_db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS projects (
          id TEXT PRIMARY KEY,
          slug TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL,
          description TEXT,
          status TEXT NOT NULL DEFAULT 'active',
          default_workflow_template_id TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workflow_templates (
          id TEXT PRIMARY KEY,
          project_id TEXT,
          name TEXT NOT NULL,
          description TEXT,
          built_in INTEGER NOT NULL DEFAULT 0,
          spec_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS workflow_steps (
          id TEXT PRIMARY KEY,
          template_id TEXT NOT NULL,
          step_key TEXT NOT NULL,
          name TEXT NOT NULL,
          kind TEXT NOT NULL,
          position INTEGER NOT NULL,
          column_key TEXT,
          role TEXT,
          profile TEXT,
          task_template TEXT,
          success_step_key TEXT,
          failure_step_key TEXT,
          retry_limit INTEGER NOT NULL DEFAULT 0,
          config_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(template_id) REFERENCES workflow_templates(id) ON DELETE CASCADE,
          UNIQUE(template_id, step_key)
        );

        CREATE TABLE IF NOT EXISTS role_assignments (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          role TEXT NOT NULL,
          profile TEXT,
          label TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
          UNIQUE(project_id, role)
        );

        CREATE TABLE IF NOT EXISTS work_items (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL,
          display_id TEXT NOT NULL,
          title TEXT NOT NULL,
          body TEXT,
          status TEXT NOT NULL DEFAULT 'triage',
          workflow_template_id TEXT,
          current_step_key TEXT,
          kanban_task_id TEXT,
          priority INTEGER NOT NULL DEFAULT 0,
          skills_json TEXT NOT NULL DEFAULT '[]',
          workspace_kind TEXT NOT NULL DEFAULT 'scratch',
          workspace_path TEXT,
          metadata_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
          FOREIGN KEY(workflow_template_id) REFERENCES workflow_templates(id) ON DELETE SET NULL,
          UNIQUE(project_id, display_id)
        );

        CREATE TABLE IF NOT EXISTS timeline_events (
          id TEXT PRIMARY KEY,
          entity_kind TEXT NOT NULL,
          entity_id TEXT NOT NULL,
          project_id TEXT,
          work_item_id TEXT,
          event_type TEXT NOT NULL,
          actor_kind TEXT,
          actor_ref TEXT,
          message TEXT,
          data_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS artifact_refs (
          id TEXT PRIMARY KEY,
          project_id TEXT,
          work_item_id TEXT,
          kind TEXT NOT NULL,
          uri TEXT NOT NULL,
          title TEXT,
          mime TEXT,
          size INTEGER,
          metadata_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        );

        INSERT OR IGNORE INTO schema_migrations(version, applied_at)
          VALUES (1, datetime('now'));
        """
    )
    conn.commit()


DEFAULT_WORKFLOW_STEPS: list[dict[str, Any]] = [
    {
        "key": "triage",
        "name": "Triage",
        "kind": "planning",
        "column": "triage",
        "role": "pm",
        "success": "design",
        "taskTemplate": "Clarify the request, scope, constraints, and acceptance criteria.",
    },
    {
        "key": "design",
        "name": "Design / Spec",
        "kind": "planning",
        "column": "todo",
        "role": "pm",
        "success": "implement",
        "failure": "triage",
        "taskTemplate": "Produce an implementation plan with risks, dependencies, and definition of done.",
    },
    {
        "key": "implement",
        "name": "Implement",
        "kind": "execution",
        "column": "ready",
        "role": "implementation",
        "success": "review",
        "failure": "blocked",
        "taskTemplate": "Implement the work packet according to the spec and local repo conventions.",
    },
    {
        "key": "review",
        "name": "Review",
        "kind": "quality",
        "column": "ready",
        "role": "review",
        "success": "approval",
        "failure": "rework",
        "taskTemplate": "Review implementation quality, tests, edge cases, and integration risk.",
    },
    {
        "key": "rework",
        "name": "Rework",
        "kind": "execution",
        "column": "todo",
        "role": "implementation",
        "success": "review",
        "failure": "blocked",
        "taskTemplate": "Address review feedback and preserve the original acceptance criteria.",
    },
    {
        "key": "approval",
        "name": "Approval",
        "kind": "human",
        "column": "blocked",
        "role": "approval",
        "success": "done",
        "failure": "rework",
        "taskTemplate": "Present final evidence and wait for human approval.",
    },
    {
        "key": "done",
        "name": "Done",
        "kind": "terminal",
        "column": "done",
        "role": "pm",
        "taskTemplate": "Work accepted and archived into project history.",
    },
]

ROLE_LABELS = {
    "pm": "Project Manager / BA",
    "implementation": "Implementation",
    "review": "Review / QA",
    "approval": "Approval",
}


def available_profiles() -> list[dict[str, Any]]:
    try:
        from hermes_cli.profiles import list_profiles

        rows = []
        for profile in list_profiles():
            name = getattr(profile, "name", "")
            if not name:
                continue
            rows.append(
                {
                    "name": name,
                    "path": str(getattr(profile, "path", "")),
                    "is_default": bool(getattr(profile, "is_default", False)),
                    "model": getattr(profile, "model", None),
                    "provider": getattr(profile, "provider", None),
                }
            )
        return sorted(rows, key=lambda r: (not r.get("is_default", False), r["name"]))
    except Exception:
        return []


def default_role_profiles() -> dict[str, str]:
    names = [p["name"] for p in available_profiles()]

    def pick(*candidates: str) -> str:
        for candidate in candidates:
            if candidate in names:
                return candidate
        return "default" if "default" in names else (names[0] if names else "")

    return {
        "pm": pick("project-manager", "pm", "default"),
        "implementation": pick("engineer-agent", "coder", "developer", "default"),
        "review": pick("reviewer-agent", "qa-agent", "reviewer", "engineer-agent", "default"),
        "approval": "human",
    }


def timeline(
    conn: sqlite3.Connection,
    *,
    entity_kind: str,
    entity_id: str,
    event_type: str,
    project_id: str | None = None,
    work_item_id: str | None = None,
    message: str | None = None,
    data: Any = None,
    actor_kind: str = "system",
    actor_ref: str = "hermes-flow",
) -> None:
    conn.execute(
        """
        INSERT INTO timeline_events (
          id, entity_kind, entity_id, project_id, work_item_id, event_type,
          actor_kind, actor_ref, message, data_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id("evt"),
            entity_kind,
            entity_id,
            project_id,
            work_item_id,
            event_type,
            actor_kind,
            actor_ref,
            message,
            json_dumps(data or {}),
            now_iso(),
        ),
    )


def next_display_id(conn: sqlite3.Connection, project_id: str) -> str:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM work_items WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    return f"WF-{int(row['n']) + 1:04d}"


def insert_default_workflow(conn: sqlite3.Connection, project_id: str, *, name: str = "Default SDLC") -> str:
    template_id = new_id("wft")
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO workflow_templates (
          id, project_id, name, description, built_in, spec_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (
            template_id,
            project_id,
            name,
            "Triage, design/spec, implement, review, rework, approval, done.",
            json_dumps({"source": "hermes-flow", "version": 1}),
            ts,
            ts,
        ),
    )
    for idx, step in enumerate(DEFAULT_WORKFLOW_STEPS):
        conn.execute(
            """
            INSERT INTO workflow_steps (
              id, template_id, step_key, name, kind, position, column_key, role,
              profile, task_template, success_step_key, failure_step_key,
              retry_limit, config_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("wfs"),
                template_id,
                step["key"],
                step["name"],
                step["kind"],
                idx,
                step.get("column"),
                step.get("role"),
                None,
                step.get("taskTemplate"),
                step.get("success"),
                step.get("failure"),
                0,
                json_dumps({}),
                ts,
                ts,
            ),
        )
    conn.execute(
        "UPDATE projects SET default_workflow_template_id = ?, updated_at = ? WHERE id = ?",
        (template_id, ts, project_id),
    )
    timeline(
        conn,
        entity_kind="project",
        entity_id=project_id,
        project_id=project_id,
        event_type="workflow.applied",
        message=f"Applied workflow template {name}.",
        data={"templateId": template_id},
    )
    return template_id


def seed_roles(conn: sqlite3.Connection, project_id: str) -> None:
    ts = now_iso()
    for role, profile in default_role_profiles().items():
        conn.execute(
            """
            INSERT OR IGNORE INTO role_assignments (
              id, project_id, role, profile, label, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id("role"), project_id, role, profile, ROLE_LABELS.get(role, role), ts, ts),
        )


def row_project(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    roles = [
        {
            "id": r["id"],
            "role": r["role"],
            "profile": r["profile"],
            "label": r["label"] or ROLE_LABELS.get(r["role"], r["role"]),
            "createdAt": r["created_at"],
            "updatedAt": r["updated_at"],
        }
        for r in conn.execute(
            "SELECT * FROM role_assignments WHERE project_id = ? ORDER BY role",
            (row["id"],),
        )
    ]
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "description": row["description"] or "",
        "status": row["status"],
        "defaultWorkflowTemplateId": row["default_workflow_template_id"],
        "roleAssignments": roles,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def row_template(conn: sqlite3.Connection, row: sqlite3.Row, *, include_steps: bool = True) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    if include_steps:
        for s in conn.execute(
            "SELECT * FROM workflow_steps WHERE template_id = ? ORDER BY position ASC",
            (row["id"],),
        ):
            steps.append(
                {
                    "id": s["id"],
                    "key": s["step_key"],
                    "name": s["name"],
                    "kind": s["kind"],
                    "position": s["position"],
                    "column": s["column_key"],
                    "role": s["role"],
                    "profile": s["profile"],
                    "taskTemplate": s["task_template"],
                    "success": s["success_step_key"],
                    "failure": s["failure_step_key"],
                    "retryLimit": s["retry_limit"],
                    "config": json_loads(s["config_json"], {}),
                    "createdAt": s["created_at"],
                    "updatedAt": s["updated_at"],
                }
            )
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "name": row["name"],
        "description": row["description"] or "",
        "builtIn": bool(row["built_in"]),
        "spec": json_loads(row["spec_json"], {}),
        "steps": steps,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def row_work_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "displayId": row["display_id"],
        "title": row["title"],
        "body": row["body"] or "",
        "status": row["status"],
        "workflowTemplateId": row["workflow_template_id"],
        "currentStepKey": row["current_step_key"],
        "kanbanTaskId": row["kanban_task_id"],
        "priority": row["priority"],
        "skills": json_loads(row["skills_json"], []),
        "workspaceKind": row["workspace_kind"],
        "workspacePath": row["workspace_path"],
        "metadata": json_loads(row["metadata_json"], {}),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


class ProjectCreate(BaseModel):
    name: str
    slug: Optional[str] = None
    description: Optional[str] = None
    roleAssignments: dict[str, Optional[str]] = Field(default_factory=dict)


class ProjectPatch(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    defaultWorkflowTemplateId: Optional[str] = None
    roleAssignments: Optional[dict[str, Optional[str]]] = None


class TemplateCreate(BaseModel):
    projectId: Optional[str] = None
    name: str
    description: Optional[str] = None
    steps: list[dict[str, Any]] = Field(default_factory=list)


class TemplatePatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    steps: Optional[list[dict[str, Any]]] = None


class ApplyTemplateBody(BaseModel):
    templateId: Optional[str] = None


class WorkItemCreate(BaseModel):
    projectId: str
    title: str
    body: Optional[str] = None
    status: str = "triage"
    workflowTemplateId: Optional[str] = None
    currentStepKey: Optional[str] = "triage"
    priority: int = 0
    skills: list[str] = Field(default_factory=list)
    workspaceKind: str = "scratch"
    workspacePath: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkItemPatch(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    status: Optional[str] = None
    workflowTemplateId: Optional[str] = None
    currentStepKey: Optional[str] = None
    priority: Optional[int] = None
    skills: Optional[list[str]] = None
    workspaceKind: Optional[str] = None
    workspacePath: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class CompileKanbanBody(BaseModel):
    status: str = "ready"
    force: bool = False
    mode: str = "workflow"


@app.get("/flow/health")
@app.get("/health")
def health():
    with connect() as conn:
        project_count = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
    return {
        "ok": True,
        "flow": {
            "ok": True,
            "pid": os.getpid(),
            "time": time.time(),
            "hermesHome": str(hermes_root()),
            "db": str(flow_db_path()),
            "projectCount": project_count,
        },
    }


def probe(url: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=1.5) as response:
            body = response.read(512 * 1024)
            data: Any
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                data = {"status": response.status}
            return {"ok": True, "url": url, "data": data}
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}


def surface_descriptors() -> list[dict[str, Any]]:
    builtins = [
        ("sessions", "dashboard", "All Chats", "/sessions", "message-square"),
        ("config", "dashboard", "Config", "/config", "settings"),
        ("env", "dashboard", "Environment", "/env", "key"),
        ("cron", "dashboard", "Cron", "/cron", "clock"),
        ("skills", "dashboard", "Skills", "/skills", "sparkles"),
        ("logs", "dashboard", "Logs", "/logs", "terminal"),
        ("analytics", "dashboard", "Analytics", "/analytics", "bar-chart"),
        ("kanban", "dashboard", "Kanban", "/kanban", "kanban"),
        ("docs", "dashboard", "API Reference", "/docs", "book-open"),
    ]
    rows = [
        {
            "id": id_,
            "kind": kind,
            "label": label,
            "title": label,
            "icon": icon,
            "source": "dashboard",
            "path": path,
            "iframeUrl": f"{DEFAULT_DASHBOARD_URL}{path}",
            "capabilities": ["iframe"],
        }
        for id_, kind, label, path, icon in builtins
    ]
    for plugin_dir in sorted((Path(__file__).resolve().parents[2] / "plugins").glob("*/dashboard")):
        plugin_name = plugin_dir.parent.name
        if plugin_name in {"flow", "kanban"}:
            continue
        label = plugin_name.replace("_", " ").replace("-", " ").title()
        path = f"/plugins/{plugin_name}"
        manifest = plugin_dir / "plugin.json"
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text())
                label = data.get("name") or data.get("label") or label
                path = data.get("path") or data.get("route") or path
            except Exception:
                pass
        rows.append(
            {
                "id": f"plugin:{plugin_name}",
                "kind": "plugin",
                "label": label,
                "title": label,
                "icon": "external-link",
                "source": "plugin",
                "pluginName": plugin_name,
                "path": path,
                "iframeUrl": f"{DEFAULT_DASHBOARD_URL}{path}",
                "capabilities": ["iframe"],
                "related": plugin_name in {"vulcan-cluster", "vulcan-beam", "hermes-cluster"},
            }
        )
    return rows


@app.get("/flow/config")
def config():
    surfaces = surface_descriptors()
    return {
        "dashboardUrl": DEFAULT_DASHBOARD_URL,
        "gatewayApiUrl": DEFAULT_GATEWAY_API_URL,
        "attachmentRoot": str(attachments_root()),
        "surfaceCount": len(surfaces),
        "maxAttachmentBytes": MAX_ATTACHMENT_BYTES,
        "advancedDb": str(flow_db_path()),
    }


@app.get("/flow/status")
def status():
    return {
        "ok": True,
        "flow": health()["flow"],
        "gatewayApi": {"name": "gateway", **probe(f"{DEFAULT_GATEWAY_API_URL}/health")},
        "dashboard": {"name": "dashboard", **probe(f"{DEFAULT_DASHBOARD_URL}/api/status")},
    }


@app.get("/flow/surfaces")
def surfaces():
    return {"surfaces": surface_descriptors()}


@app.get("/flow/projects")
def list_projects():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY updated_at DESC, name ASC").fetchall()
        return {"projects": [row_project(conn, row) for row in rows], "profiles": available_profiles()}


@app.post("/flow/projects")
def create_project(payload: ProjectCreate):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    slug = slugify(payload.slug or name)
    with connect() as conn:
        try:
            project_id = new_id("proj")
            ts = now_iso()
            conn.execute(
                """
                INSERT INTO projects (id, slug, name, description, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'active', ?, ?)
                """,
                (project_id, slug, name, payload.description or "", ts, ts),
            )
            seed_roles(conn, project_id)
            if payload.roleAssignments:
                patch_role_assignments(conn, project_id, payload.roleAssignments)
            template_id = insert_default_workflow(conn, project_id)
            timeline(
                conn,
                entity_kind="project",
                entity_id=project_id,
                project_id=project_id,
                event_type="project.created",
                message=f"Created project {name}.",
                data={"slug": slug, "kanbanBoard": slug, "defaultWorkflowTemplateId": template_id},
            )
            ensure_kanban_board(slug, name=name, description=payload.description or "")
            conn.commit()
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            return {"project": row_project(conn, row)}
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail=f"project slug already exists: {slug}") from exc


def patch_role_assignments(conn: sqlite3.Connection, project_id: str, patch: dict[str, Optional[str]]) -> None:
    ts = now_iso()
    for role, profile in patch.items():
        role_key = role.strip()
        if not role_key:
            continue
        conn.execute(
            """
            INSERT INTO role_assignments (id, project_id, role, profile, label, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, role) DO UPDATE SET
              profile = excluded.profile,
              label = excluded.label,
              updated_at = excluded.updated_at
            """,
            (
                new_id("role"),
                project_id,
                role_key,
                (profile or "").strip() or None,
                ROLE_LABELS.get(role_key, role_key),
                ts,
                ts,
            ),
        )
    timeline(
        conn,
        entity_kind="project",
        entity_id=project_id,
        project_id=project_id,
        event_type="roles.updated",
        message="Updated role/profile routing.",
        data=patch,
    )


@app.patch("/flow/projects/{project_id}")
def update_project(project_id: str, payload: ProjectPatch):
    with connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        updates: list[str] = []
        values: list[Any] = []
        for api_key, db_key in [
            ("name", "name"),
            ("slug", "slug"),
            ("description", "description"),
            ("status", "status"),
            ("defaultWorkflowTemplateId", "default_workflow_template_id"),
        ]:
            value = getattr(payload, api_key)
            if value is not None:
                updates.append(f"{db_key} = ?")
                values.append(slugify(value) if api_key == "slug" else value)
        if updates:
            updates.append("updated_at = ?")
            values.append(now_iso())
            values.append(project_id)
            try:
                conn.execute(f"UPDATE projects SET {', '.join(updates)} WHERE id = ?", values)
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=409, detail="project slug already exists") from exc
        if payload.roleAssignments is not None:
            patch_role_assignments(conn, project_id, payload.roleAssignments)
        updated_project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        ensure_kanban_board(
            updated_project["slug"],
            name=updated_project["name"],
            description=updated_project["description"] or "",
        )
        timeline(
            conn,
            entity_kind="project",
            entity_id=project_id,
            project_id=project_id,
            event_type="project.updated",
            message="Updated project.",
            data=payload.model_dump(exclude_unset=True),
        )
        conn.commit()
        return {"project": row_project(conn, conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())}


@app.get("/flow/workflows/templates")
def list_templates(project_id: Optional[str] = Query(None)):
    with connect() as conn:
        if project_id:
            rows = conn.execute(
                "SELECT * FROM workflow_templates WHERE project_id = ? OR project_id IS NULL ORDER BY built_in DESC, name",
                (project_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM workflow_templates ORDER BY built_in DESC, name").fetchall()
        return {"templates": [row_template(conn, row) for row in rows]}


@app.post("/flow/workflows/templates")
def create_template(payload: TemplateCreate):
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    template_id = new_id("wft")
    ts = now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO workflow_templates (id, project_id, name, description, built_in, spec_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (template_id, payload.projectId, payload.name.strip(), payload.description or "", json_dumps({}), ts, ts),
        )
        upsert_steps(conn, template_id, payload.steps)
        conn.commit()
        return {"template": row_template(conn, conn.execute("SELECT * FROM workflow_templates WHERE id = ?", (template_id,)).fetchone())}


def upsert_steps(conn: sqlite3.Connection, template_id: str, steps: list[dict[str, Any]]) -> None:
    ts = now_iso()
    conn.execute("DELETE FROM workflow_steps WHERE template_id = ?", (template_id,))
    for idx, step in enumerate(steps or DEFAULT_WORKFLOW_STEPS):
        key = str(step.get("key") or step.get("step_key") or f"step-{idx + 1}").strip()
        conn.execute(
            """
            INSERT INTO workflow_steps (
              id, template_id, step_key, name, kind, position, column_key, role,
              profile, task_template, success_step_key, failure_step_key,
              retry_limit, config_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("wfs"),
                template_id,
                key,
                step.get("name") or key,
                step.get("kind") or "execution",
                int(step.get("position", idx)),
                step.get("column") or step.get("column_key"),
                step.get("role"),
                step.get("profile"),
                step.get("taskTemplate") or step.get("task_template"),
                step.get("success") or step.get("success_step_key"),
                step.get("failure") or step.get("failure_step_key"),
                int(step.get("retryLimit") or step.get("retry_limit") or 0),
                json_dumps(step.get("config") or {}),
                ts,
                ts,
            ),
        )


@app.patch("/flow/workflows/templates/{template_id}")
def update_template(template_id: str, payload: TemplatePatch):
    with connect() as conn:
        row = conn.execute("SELECT * FROM workflow_templates WHERE id = ?", (template_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="template not found")
        updates: list[str] = []
        values: list[Any] = []
        if payload.name is not None:
            updates.append("name = ?")
            values.append(payload.name)
        if payload.description is not None:
            updates.append("description = ?")
            values.append(payload.description)
        if updates:
            updates.append("updated_at = ?")
            values.append(now_iso())
            values.append(template_id)
            conn.execute(f"UPDATE workflow_templates SET {', '.join(updates)} WHERE id = ?", values)
        if payload.steps is not None:
            upsert_steps(conn, template_id, payload.steps)
        conn.commit()
        return {"template": row_template(conn, conn.execute("SELECT * FROM workflow_templates WHERE id = ?", (template_id,)).fetchone())}


@app.post("/flow/projects/{project_id}/apply-template")
def apply_template(project_id: str, payload: ApplyTemplateBody):
    with connect() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        if payload.templateId:
            template = conn.execute("SELECT * FROM workflow_templates WHERE id = ?", (payload.templateId,)).fetchone()
            if template is None:
                raise HTTPException(status_code=404, detail="template not found")
            template_id = payload.templateId
            conn.execute(
                "UPDATE projects SET default_workflow_template_id = ?, updated_at = ? WHERE id = ?",
                (template_id, now_iso(), project_id),
            )
        else:
            template_id = insert_default_workflow(conn, project_id)
        conn.commit()
        row = conn.execute("SELECT * FROM workflow_templates WHERE id = ?", (template_id,)).fetchone()
        return {"template": row_template(conn, row), "project": row_project(conn, conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())}


@app.get("/flow/work-items")
def list_work_items(project_id: Optional[str] = Query(None)):
    with connect() as conn:
        if project_id:
            rows = conn.execute(
                "SELECT * FROM work_items WHERE project_id = ? ORDER BY updated_at DESC",
                (project_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM work_items ORDER BY updated_at DESC LIMIT 200").fetchall()
        return {"workItems": [row_work_item(row) for row in rows]}


@app.post("/flow/work-items")
def create_work_item(payload: WorkItemCreate):
    if not payload.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    with connect() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (payload.projectId,)).fetchone()
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        item_id = new_id("wi")
        ts = now_iso()
        template_id = payload.workflowTemplateId or project["default_workflow_template_id"]
        display_id = next_display_id(conn, payload.projectId)
        conn.execute(
            """
            INSERT INTO work_items (
              id, project_id, display_id, title, body, status, workflow_template_id,
              current_step_key, priority, skills_json, workspace_kind, workspace_path,
              metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                payload.projectId,
                display_id,
                payload.title.strip(),
                payload.body or "",
                payload.status,
                template_id,
                payload.currentStepKey or "triage",
                payload.priority,
                json_dumps(payload.skills),
                payload.workspaceKind,
                payload.workspacePath,
                json_dumps(payload.metadata),
                ts,
                ts,
            ),
        )
        timeline(
            conn,
            entity_kind="work_item",
            entity_id=item_id,
            project_id=payload.projectId,
            work_item_id=item_id,
            event_type="work_item.created",
            message=f"Created work item {display_id}.",
            data={"title": payload.title},
        )
        conn.commit()
        return {"workItem": row_work_item(conn.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone())}


@app.patch("/flow/work-items/{work_item_id}")
def update_work_item(work_item_id: str, payload: WorkItemPatch):
    with connect() as conn:
        row = conn.execute("SELECT * FROM work_items WHERE id = ?", (work_item_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="work item not found")
        updates: list[str] = []
        values: list[Any] = []
        mapping = {
            "title": "title",
            "body": "body",
            "status": "status",
            "workflowTemplateId": "workflow_template_id",
            "currentStepKey": "current_step_key",
            "priority": "priority",
            "workspaceKind": "workspace_kind",
            "workspacePath": "workspace_path",
        }
        for api_key, db_key in mapping.items():
            value = getattr(payload, api_key)
            if value is not None:
                updates.append(f"{db_key} = ?")
                values.append(value)
        if payload.skills is not None:
            updates.append("skills_json = ?")
            values.append(json_dumps(payload.skills))
        if payload.metadata is not None:
            updates.append("metadata_json = ?")
            values.append(json_dumps(payload.metadata))
        if updates:
            updates.append("updated_at = ?")
            values.append(now_iso())
            values.append(work_item_id)
            conn.execute(f"UPDATE work_items SET {', '.join(updates)} WHERE id = ?", values)
            timeline(
                conn,
                entity_kind="work_item",
                entity_id=work_item_id,
                project_id=row["project_id"],
                work_item_id=work_item_id,
                event_type="work_item.updated",
                message="Updated work item.",
                data=payload.model_dump(exclude_unset=True),
            )
        conn.commit()
        return {"workItem": row_work_item(conn.execute("SELECT * FROM work_items WHERE id = ?", (work_item_id,)).fetchone())}


def _profile_for_role(conn: sqlite3.Connection, project_id: str, role: str | None, explicit_profile: str | None = None) -> str | None:
    profile = explicit_profile
    if role and not profile:
        assignment = conn.execute(
            "SELECT profile FROM role_assignments WHERE project_id = ? AND role = ?",
            (project_id, role),
        ).fetchone()
        profile = assignment["profile"] if assignment else None
    if not profile or profile == "human":
        return None
    return profile


def role_for_step(conn: sqlite3.Connection, project_id: str, template_id: str | None, step_key: str | None) -> tuple[str | None, str | None, str | None]:
    if not template_id or not step_key:
        return None, None, None
    step = conn.execute(
        "SELECT * FROM workflow_steps WHERE template_id = ? AND step_key = ?",
        (template_id, step_key),
    ).fetchone()
    if step is None:
        return None, None, None
    role = step["role"]
    profile = _profile_for_role(conn, project_id, role, step["profile"])
    return role, profile, step["task_template"]


def workflow_path(conn: sqlite3.Connection, template_id: str | None, start_step_key: str | None) -> list[sqlite3.Row]:
    if not template_id:
        return []
    steps = conn.execute(
        "SELECT * FROM workflow_steps WHERE template_id = ? ORDER BY position ASC",
        (template_id,),
    ).fetchall()
    if not steps:
        return []
    by_key = {step["step_key"]: step for step in steps}
    current = start_step_key if start_step_key in by_key else steps[0]["step_key"]
    path: list[sqlite3.Row] = []
    seen: set[str] = set()
    while current and current in by_key and current not in seen:
        seen.add(current)
        step = by_key[current]
        if step["kind"] == "terminal":
            break
        path.append(step)
        current = step["success_step_key"]
    return path


def kanban_task_dict(task: Any, task_id: str) -> dict[str, Any]:
    return task.__dict__ if task else {"id": task_id}


def compile_task_body(
    *,
    item: sqlite3.Row,
    project: sqlite3.Row,
    step: sqlite3.Row | None,
    role: str | None,
    task_template: str | None,
) -> str:
    body_parts = [
        f"Flow work item: {item['display_id']}",
        f"Project: {project['name']} ({project['slug']})",
    ]
    if step is not None:
        body_parts.append(f"Workflow step: {step['name']} ({step['step_key']})")
        if step["success_step_key"]:
            body_parts.append(f"Success hands off to: {step['success_step_key']}")
        if step["failure_step_key"]:
            body_parts.append(f"Failure/rework path: {step['failure_step_key']}")
    elif item["current_step_key"]:
        body_parts.append(f"Workflow step: {item['current_step_key']}")
    if role:
        body_parts.append(f"Role: {role}")
    if task_template:
        body_parts.append(f"Step guidance: {task_template}")
    if item["body"]:
        body_parts.append("\nBrief:\n" + item["body"])
    body_parts.append(
        "\nHandoff:\nWhen completing this task, include a concise summary and structured metadata. "
        "Downstream Kanban tasks depend on this task and will receive that handoff context."
    )
    return "\n\n".join(body_parts)


@app.post("/flow/work-items/{work_item_id}/compile-kanban")
def compile_kanban(work_item_id: str, payload: CompileKanbanBody):
    with connect() as conn:
        item = conn.execute("SELECT * FROM work_items WHERE id = ?", (work_item_id,)).fetchone()
        if item is None:
            raise HTTPException(status_code=404, detail="work item not found")
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (item["project_id"],)).fetchone()
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")

        mode = payload.mode if payload.mode in {"workflow", "current-step"} else "workflow"
        step_path = workflow_path(conn, item["workflow_template_id"], item["current_step_key"]) if mode == "workflow" else []
        if not step_path:
            mode = "current-step"
            role, profile, task_template = role_for_step(
                conn,
                item["project_id"],
                item["workflow_template_id"],
                item["current_step_key"],
            )
            step_path = []
        else:
            role = profile = task_template = None

        metadata = json_loads(item["metadata_json"], {})
        flow_meta = metadata.get("flow") if isinstance(metadata, dict) else None
        if not isinstance(flow_meta, dict):
            flow_meta = {}
        flow_meta.update(
            {
                "projectId": project["id"],
                "projectSlug": project["slug"],
                "workItemId": item["id"],
                "displayId": item["display_id"],
                "workflowTemplateId": item["workflow_template_id"],
                "stepKey": item["current_step_key"],
                "compileMode": mode,
                "kanbanBoard": project["slug"],
            }
        )
        metadata["flow"] = flow_meta
        task_tenant = flow_meta.get("tenant")
        if not isinstance(task_tenant, str) or not task_tenant.strip():
            task_tenant = None

        compiled_records: list[dict[str, Any]] = []
        kanban_tasks: list[dict[str, Any]] = []
        try:
            from hermes_cli import kanban_db

            ensure_kanban_board(project["slug"], name=project["name"], description=project["description"] or "")
            kanban_db.init_db(board=project["slug"])
            kconn = kanban_db.connect(board=project["slug"])
            try:
                if mode == "current-step":
                    task_id = kanban_db.create_task(
                        kconn,
                        title=f"{item['display_id']}: {item['title']}",
                        body=compile_task_body(
                            item=item,
                            project=project,
                            step=None,
                            role=role,
                            task_template=task_template,
                        ),
                        assignee=profile,
                        created_by="hermes-flow",
                        workspace_kind=item["workspace_kind"] or "scratch",
                        workspace_path=item["workspace_path"],
                        tenant=task_tenant,
                        priority=int(item["priority"] or 0),
                        triage=payload.status == "triage",
                        idempotency_key=f"flow:{item['id']}",
                        skills=json_loads(item["skills_json"], []),
                    )
                    if payload.status not in {"triage", "ready"}:
                        # create_task starts ready by default. Park single-card
                        # projections wherever the caller explicitly asked.
                        with kanban_db.write_txn(kconn):
                            kconn.execute("UPDATE tasks SET status = ? WHERE id = ?", (payload.status, task_id))
                    task = kanban_db.get_task(kconn, task_id)
                    task_data = kanban_task_dict(task, task_id)
                    compiled_records.append(
                        {
                            "taskId": task_id,
                            "board": project["slug"],
                            "stepKey": item["current_step_key"],
                            "name": item["current_step_key"] or "Work",
                            "role": role,
                            "assignee": profile,
                            "status": task_data.get("status"),
                            "parentTaskId": None,
                        }
                    )
                    kanban_tasks.append(task_data)
                else:
                    parent_task_id: str | None = None
                    first_status = payload.status if payload.status in {"triage", "ready"} else "ready"
                    for idx, step in enumerate(step_path):
                        step_role = step["role"]
                        step_profile = _profile_for_role(conn, item["project_id"], step_role, step["profile"])
                        task_id = kanban_db.create_task(
                            kconn,
                            title=f"{item['display_id']} / {step['name']}: {item['title']}",
                            body=compile_task_body(
                                item=item,
                                project=project,
                                step=step,
                                role=step_role,
                                task_template=step["task_template"],
                            ),
                            assignee=step_profile,
                            created_by="hermes-flow",
                            workspace_kind=item["workspace_kind"] or "scratch",
                            workspace_path=item["workspace_path"],
                            tenant=task_tenant,
                            priority=int(item["priority"] or 0),
                            parents=[parent_task_id] if parent_task_id else [],
                            triage=idx == 0 and first_status == "triage",
                            idempotency_key=f"flow:{item['id']}:{step['step_key']}",
                            skills=json_loads(item["skills_json"], []),
                        )
                        if parent_task_id:
                            kanban_db.link_tasks(kconn, parent_task_id, task_id)
                        task = kanban_db.get_task(kconn, task_id)
                        task_data = kanban_task_dict(task, task_id)
                        compiled_records.append(
                            {
                                "taskId": task_id,
                                "board": project["slug"],
                                "stepKey": step["step_key"],
                                "name": step["name"],
                                "kind": step["kind"],
                                "role": step_role,
                                "assignee": step_profile,
                                "status": task_data.get("status"),
                                "parentTaskId": parent_task_id,
                            }
                        )
                        kanban_tasks.append(task_data)
                        parent_task_id = task_id
                    kanban_db.recompute_ready(kconn)
                    for idx, record in enumerate(compiled_records):
                        task = kanban_db.get_task(kconn, record["taskId"])
                        task_data = kanban_task_dict(task, record["taskId"])
                        record["status"] = task_data.get("status")
                        kanban_tasks[idx] = task_data
            finally:
                kconn.close()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"kanban projection failed: {exc}") from exc
        task_id = compiled_records[0]["taskId"] if compiled_records else None
        if not task_id:
            raise HTTPException(status_code=500, detail="kanban projection produced no tasks")
        flow_meta["kanbanTasks"] = compiled_records
        flow_meta["compiledAt"] = now_iso()
        ts = now_iso()
        conn.execute(
            "UPDATE work_items SET kanban_task_id = ?, metadata_json = ?, updated_at = ? WHERE id = ?",
            (task_id, json_dumps(metadata), ts, item["id"]),
        )
        timeline(
            conn,
            entity_kind="work_item",
            entity_id=item["id"],
            project_id=project["id"],
            work_item_id=item["id"],
            event_type="kanban.compiled",
            message=(
                f"Compiled to Kanban workflow ({len(compiled_records)} tasks)."
                if mode == "workflow"
                else f"Compiled to Kanban task {task_id}."
            ),
            data={"taskId": task_id, "tasks": compiled_records, "projectSlug": project["slug"], "kanbanBoard": project["slug"]},
        )
        conn.commit()
        return {
            "workItem": row_work_item(conn.execute("SELECT * FROM work_items WHERE id = ?", (item["id"],)).fetchone()),
            "kanbanTask": kanban_tasks[0] if kanban_tasks else {"id": task_id},
            "kanbanTasks": kanban_tasks,
        }


@app.get("/flow/work-items/{work_item_id}/timeline")
def work_item_timeline(work_item_id: str):
    with connect() as conn:
        item = conn.execute("SELECT * FROM work_items WHERE id = ?", (work_item_id,)).fetchone()
        if item is None:
            raise HTTPException(status_code=404, detail="work item not found")
        rows = conn.execute(
            "SELECT * FROM timeline_events WHERE work_item_id = ? OR entity_id = ? ORDER BY created_at ASC",
            (work_item_id, work_item_id),
        ).fetchall()
        return {
            "events": [
                {
                    "id": r["id"],
                    "entityKind": r["entity_kind"],
                    "entityId": r["entity_id"],
                    "projectId": r["project_id"],
                    "workItemId": r["work_item_id"],
                    "eventType": r["event_type"],
                    "actorKind": r["actor_kind"],
                    "actorRef": r["actor_ref"],
                    "message": r["message"],
                    "data": json_loads(r["data_json"], {}),
                    "createdAt": r["created_at"],
                }
                for r in rows
            ]
        }


def env_path() -> Path:
    return hermes_root() / ".env"


@app.get("/flow/env")
def flow_env():
    from hermes_cli.config import OPTIONAL_ENV_VARS, load_env, redact_key

    env_on_disk = load_env()
    variables: dict[str, Any] = {}
    known = set(OPTIONAL_ENV_VARS)
    for var_name, info in OPTIONAL_ENV_VARS.items():
        value = env_on_disk.get(var_name) or os.environ.get(var_name)
        variables[var_name] = {
            "is_set": bool(value),
            "redacted_value": redact_key(value) if value else None,
            "description": info.get("description", ""),
            "url": info.get("url"),
            "category": info.get("category", ""),
            "is_password": info.get("password", False),
            "tools": info.get("tools", []),
            "advanced": info.get("advanced", False),
        }
    for key, value in sorted(env_on_disk.items()):
        if key in known:
            continue
        variables[key] = {
            "is_set": bool(value),
            "redacted_value": redact_key(value) if value else None,
            "description": "Custom environment variable",
            "url": None,
            "category": "custom",
            "is_password": True,
            "tools": [],
            "advanced": False,
            "custom": True,
        }
    return {"envPath": str(env_path()), "variables": variables}


class EnvUpdate(BaseModel):
    key: str
    value: str


class EnvDelete(BaseModel):
    key: str


@app.put("/flow/env")
def set_env(payload: EnvUpdate):
    from hermes_cli.config import save_env_value

    save_env_value(payload.key, payload.value)
    return {"ok": True, "key": payload.key}


@app.delete("/flow/env")
def delete_env(payload: EnvDelete):
    from hermes_cli.config import remove_env_value

    if not remove_env_value(payload.key):
        raise HTTPException(status_code=404, detail=f"{payload.key} not found in .env")
    return {"ok": True, "key": payload.key}


@app.post("/flow/env/reveal")
def reveal_env(payload: EnvDelete):
    from hermes_cli.config import get_env_value

    value = get_env_value(payload.key)
    if value is None:
        raise HTTPException(status_code=404, detail=f"{payload.key} not found")
    return {"key": payload.key, "value": value}


@app.get("/flow/providers/oauth")
def oauth_providers():
    return {
        "providers": [
            {
                "id": "anthropic",
                "name": "Anthropic (Claude API)",
                "flow": "pkce",
                "cli_command": "hermes auth add anthropic",
                "docs_url": "https://docs.anthropic.com/",
                "status": {"logged_in": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_TOKEN"))},
            },
            {
                "id": "openai-codex",
                "name": "OpenAI Codex (ChatGPT)",
                "flow": "device_code",
                "cli_command": "hermes auth add openai-codex",
                "docs_url": None,
                "status": {"logged_in": bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("CHATGPT_TOKEN"))},
            },
            {
                "id": "nous",
                "name": "Nous Portal",
                "flow": "device_code",
                "cli_command": "hermes auth add nous",
                "docs_url": None,
                "status": {"logged_in": bool(os.environ.get("NOUS_API_KEY") or os.environ.get("NOUS_TOKEN"))},
            },
        ]
    }


@app.post("/flow/providers/oauth/{provider_id}/start")
def oauth_start(provider_id: str):
    raise HTTPException(status_code=501, detail=f"Use the CLI for now: hermes auth add {provider_id}")


@app.post("/flow/providers/oauth/{provider_id}/submit")
def oauth_submit(provider_id: str):
    raise HTTPException(status_code=501, detail=f"Use the CLI for now: hermes auth add {provider_id}")


@app.get("/flow/providers/oauth/{provider_id}/poll/{session_id}")
def oauth_poll(provider_id: str, session_id: str):
    return {"session_id": session_id, "status": "error", "error_message": "Browser OAuth bridge is not enabled in the Flow sidecar yet."}


@app.delete("/flow/providers/oauth/sessions/{session_id}")
def oauth_cancel(session_id: str):
    return {"ok": True}


@app.delete("/flow/providers/oauth/{provider_id}")
def oauth_disconnect(provider_id: str):
    raise HTTPException(status_code=501, detail=f"Use the CLI for now: hermes auth remove {provider_id}")


@app.get("/flow/sessions")
def sessions(limit: int = 50, offset: int = 0):
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        rows = db.list_sessions_rich(limit=limit, offset=offset)
        total = db.session_count()
        current = time.time()
        for row in rows:
            row["is_active"] = row.get("ended_at") is None and (current - row.get("last_active", row.get("started_at", 0))) < 300
        return {"sessions": rows, "total": total, "limit": limit, "offset": offset}
    finally:
        db.close()


@app.get("/flow/sessions/search")
def search_sessions(q: str = "", limit: int = 40):
    if not q.strip():
        return {"results": []}
    from hermes_state import SessionDB

    terms = []
    for token in re.findall(r'"[^"]*"|\S+', q.strip()):
        terms.append(token if token.startswith('"') or token.endswith("*") else f"{token}*")
    db = SessionDB()
    try:
        matches = db.search_messages(query=" ".join(terms), limit=limit)
        seen: dict[str, dict[str, Any]] = {}
        for match in matches:
            sid = match["session_id"]
            if sid not in seen:
                seen[sid] = {
                    "session_id": sid,
                    "snippet": match.get("snippet", ""),
                    "role": match.get("role"),
                    "source": match.get("source"),
                    "model": match.get("model"),
                    "session_started": match.get("session_started"),
                }
        return {"results": list(seen.values())}
    finally:
        db.close()


@app.get("/flow/sessions/{session_id}/messages")
def session_messages(session_id: str):
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        sid = db.resolve_session_id(session_id)
        if not sid:
            raise HTTPException(status_code=404, detail="Session not found")
        return {"session_id": sid, "messages": db.get_messages(sid)}
    finally:
        db.close()


@app.delete("/flow/sessions/{session_id}")
def delete_session(session_id: str):
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        if not db.delete_session(session_id):
            raise HTTPException(status_code=404, detail="Session not found")
        return {"ok": True}
    finally:
        db.close()


def skill_rows() -> list[dict[str, Any]]:
    from hermes_cli.config import load_config
    from hermes_cli.skills_config import get_disabled_skills
    from tools.skills_tool import _find_all_skills

    disabled = get_disabled_skills(load_config())
    rows = []
    for skill in _find_all_skills(skip_disabled=False):
        path = Path(skill.get("path") or skill.get("skill_file") or "")
        skill_file = Path(skill.get("skill_file") or path / "SKILL.md")
        try:
            size = skill_file.stat().st_size
            updated = datetime.fromtimestamp(skill_file.stat().st_mtime, timezone.utc).isoformat()
        except Exception:
            size = 0
            updated = ""
        name = skill.get("name") or path.name
        rows.append(
            {
                "name": name,
                "description": skill.get("description") or "",
                "category": skill.get("category"),
                "enabled": name not in disabled,
                "path": str(path),
                "skillFile": str(skill_file),
                "source": skill.get("source") or "local",
                "readOnly": not os.access(skill_file, os.W_OK),
                "size": size,
                "updatedAt": updated,
                "tags": skill.get("tags") or [],
            }
        )
    return sorted(rows, key=lambda r: r["name"])


@app.get("/flow/skills")
def skills():
    rows = skill_rows()
    try:
        from hermes_cli.tools_config import _get_effective_configurable_toolsets

        toolsets = _get_effective_configurable_toolsets()
    except Exception:
        toolsets = []
    return {
        "skills": rows,
        "toolsets": toolsets,
        "enabledCount": sum(1 for row in rows if row["enabled"]),
        "totalCount": len(rows),
    }


@app.get("/flow/skills/{name}")
def skill_detail(name: str):
    row = next((r for r in skill_rows() if r["name"] == name), None)
    if row is None:
        raise HTTPException(status_code=404, detail="skill not found")
    path = Path(row["skillFile"])
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    return {"skill": row, "content": content, "canSave": not row["readOnly"]}


class SkillSave(BaseModel):
    content: str


@app.put("/flow/skills/{name}")
def save_skill(name: str, payload: SkillSave):
    row = next((r for r in skill_rows() if r["name"] == name), None)
    if row is None:
        raise HTTPException(status_code=404, detail="skill not found")
    if row["readOnly"]:
        raise HTTPException(status_code=403, detail="skill is read-only")
    Path(row["skillFile"]).write_text(payload.content, encoding="utf-8")
    updated = next((r for r in skill_rows() if r["name"] == name), row)
    return {"ok": True, "skill": updated}


class SkillToggle(BaseModel):
    name: str
    enabled: bool


@app.put("/flow/skills/{name}/toggle")
def toggle_skill(name: str, payload: SkillToggle):
    from hermes_cli.config import load_config
    from hermes_cli.skills_config import get_disabled_skills, save_disabled_skills

    config = load_config()
    disabled = get_disabled_skills(config)
    if payload.enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    save_disabled_skills(config, disabled)
    return {"ok": True, "name": name, "enabled": payload.enabled}


@app.post("/flow/attachments")
async def upload_attachment(file: UploadFile = File(...), session_id: str | None = Form(None)):
    root = attachments_root()
    data = await file.read()
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="attachment too large")
    digest = hashlib.sha256(data).hexdigest()
    attachment_id = new_id("att")
    suffix = Path(file.filename or "upload").suffix
    target_dir = root / datetime.now().strftime("%Y/%m/%d")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{attachment_id}{suffix}"
    target.write_bytes(data)
    meta = {
        "id": attachment_id,
        "name": file.filename or attachment_id,
        "mime": file.content_type or "application/octet-stream",
        "size": len(data),
        "sha256": digest,
        "uri": f"/attachments/{target.relative_to(root).as_posix()}",
        "path": str(target),
        "createdAt": now_iso(),
        "sessionId": session_id,
    }
    (target.with_suffix(target.suffix + ".json")).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {"attachment": meta}


@app.get("/flow/attachments/{path:path}")
def get_attachment(path: str):
    target = (attachments_root() / path).resolve()
    if not str(target).startswith(str(attachments_root().resolve())) or not target.exists():
        raise HTTPException(status_code=404, detail="attachment not found")
    return {"path": str(target)}
