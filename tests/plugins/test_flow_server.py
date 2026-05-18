from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def _client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    module = importlib.import_module("plugins.flow.server")
    return TestClient(module.app)


def test_flow_migration_and_project_defaults(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    assert client.get("/flow/health").status_code == 200
    created = client.post(
        "/flow/projects",
        json={"name": "Project OS Test", "description": "advanced flow"},
    )
    assert created.status_code == 200
    project = created.json()["project"]
    assert project["slug"] == "project-os-test"
    assert project["defaultWorkflowTemplateId"]
    assert {r["role"] for r in project["roleAssignments"]} >= {
        "pm",
        "implementation",
        "review",
        "approval",
    }

    templates = client.get(f"/flow/workflows/templates?project_id={project['id']}").json()["templates"]
    assert templates[0]["steps"][0]["key"] == "triage"
    assert [s["key"] for s in templates[0]["steps"]][-1] == "done"

    from hermes_cli import kanban_db

    boards = {board["slug"] for board in kanban_db.list_boards()}
    assert project["slug"] in boards


def test_flow_work_item_compile_projects_to_kanban(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    project = client.post("/flow/projects", json={"name": "Compile Project"}).json()["project"]
    work = client.post(
        "/flow/work-items",
        json={
            "projectId": project["id"],
            "title": "Compile me",
            "body": "Make a projected task.",
            "currentStepKey": "triage",
            "skills": ["kanban-worker"],
        },
    )
    assert work.status_code == 200
    item = work.json()["workItem"]

    compiled = client.post(f"/flow/work-items/{item['id']}/compile-kanban", json={"mode": "workflow"})
    assert compiled.status_code == 200
    payload = compiled.json()
    assert payload["workItem"]["kanbanTaskId"]
    assert len(payload["kanbanTasks"]) == 5
    assert payload["kanbanTask"]["tenant"] is None
    assert payload["workItem"]["metadata"]["flow"]["kanbanBoard"] == project["slug"]
    assert [task["status"] for task in payload["kanbanTasks"]] == ["ready", "todo", "todo", "todo", "todo"]
    assert payload["workItem"]["metadata"]["flow"]["compileMode"] == "workflow"
    assert {task["board"] for task in payload["workItem"]["metadata"]["flow"]["kanbanTasks"]} == {project["slug"]}
    assert [task["stepKey"] for task in payload["workItem"]["metadata"]["flow"]["kanbanTasks"]] == [
        "triage",
        "design",
        "implement",
        "review",
        "approval",
    ]

    from hermes_cli import kanban_db

    kconn = kanban_db.connect(board=project["slug"])
    try:
        task_ids = [task["id"] for task in payload["kanbanTasks"]]
        links = kconn.execute(
            "SELECT parent_id, child_id FROM task_links WHERE parent_id IN ({}) ORDER BY parent_id, child_id".format(
                ",".join("?" * len(task_ids))
            ),
            task_ids,
        ).fetchall()
        assert len(links) == 4
    finally:
        kconn.close()

    default_conn = kanban_db.connect(board="default")
    try:
        assert default_conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 0
    finally:
        default_conn.close()

    timeline = client.get(f"/flow/work-items/{item['id']}/timeline").json()["events"]
    assert any(event["eventType"] == "kanban.compiled" for event in timeline)
