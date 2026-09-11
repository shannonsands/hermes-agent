"""Shared text projections for Wisdom security and professionalism checks."""

from __future__ import annotations

from typing import Any

from .professionalism import CHECK_LABELS, review_text

_STATUS_PRESENTATION = {
    "pass": ("✅", "Pass"),
    "advisory": ("⚠️", "Advisory"),
    "blocked": ("❌", "Blocked"),
    "pending": ("⏳", "Pending"),
    "retry": ("⏳", "Pending"),
    "running": ("⏳", "Pending"),
    "unavailable": ("➖", "Unavailable"),
}


def review_status_text(status: object) -> str:
    """Return the shared icon-and-text label for a review status."""

    icon, label = _STATUS_PRESENTATION.get(
        str(status or "unavailable").lower(),
        _STATUS_PRESENTATION["unavailable"],
    )
    return f"{icon} {label}"


def review_check_line(label: str, status: object) -> str:
    """Lead with the status icon; retain explicit labels for non-passing checks."""
    icon, state = _STATUS_PRESENTATION.get(
        str(status or "unavailable").lower(), _STATUS_PRESENTATION["unavailable"]
    )
    if state == "Pass":
        label = {
            "Security check": "No security issues detected",
            "Security check (local preflight)": "No security issues detected",
            "Private keys": "No private keys detected",
            "Live credentials": "No live credentials detected",
            "Secret-like assignments": "No secret key assignments",
            "Harmful instruction patterns": "No harmful instruction patterns",
        }.get(label, label)
    return f"{icon} {label}" + (f": {state}" if state != "Pass" else "")


def review_summary_text(summary: str) -> str:
    if summary.strip() == "No known matches detected.":
        return "No issues detected"
    local_summary = "Local security checks found no known matches."
    if summary.startswith(local_summary):
        summary = "No issues detected by local security checks." + summary[len(local_summary):]
    return summary[:512]


def aggregate_review_text(
    security: dict[str, Any] | None,
    professionalism: dict[str, Any] | None,
) -> str:
    return (
        review_check_line("Security check", (security or {}).get("status"))
        + " · "
        + professionalism_review_text(
            {"status": (professionalism or {}).get("status")}, include_checks=False,
        )
    )


def sharing_checks_passed(facts: dict[str, Any]) -> bool:
    """Summarize only complete saved evidence, never infer success from missing rows."""
    security = facts.get("security_check") or {}
    professionalism = facts.get("professionalism_check") or {}
    if security.get("status") != "pass" or professionalism.get("status") != "pass":
        return False
    if security.get("source") == "local_preflight" and security.get("local_status") != "pass":
        return False
    if security.get("upload_allowed") is False:
        return False
    required_security = (
        {"private_keys", "live_credentials", "secret_assignments", "skills_guard"}
        if security.get("source") == "local_preflight" else
        {"private_keys", "live_credentials", "organization_policy", "personal_information", "secret_like_assignments"}
    )
    for check, required in ((security, required_security), (professionalism, set(CHECK_LABELS))):
        rows = check.get("checks")
        if not isinstance(rows, list) or not rows:
            return False
        if any(not isinstance(row, dict) or row.get("status") != "pass"
               or row.get("finding_count") != 0 for row in rows):
            return False
        keys = [row.get("key") for row in rows]
        if len(set(keys)) != len(keys) or not required.issubset(keys):
            return False
    return True


def review_card_text(facts: dict[str, Any], expanded: bool = False, *, current: bool = True) -> str:
    """Keep native cards compact while making both full checklists accessible."""
    if current and sharing_checks_passed(facts):
        if not expanded:
            return "✅ Safe To Share (security and professionalism checks all passed)"
        details = full_review_text(facts["security_check"], facts["professionalism_check"])
        return "✅ Safe To Share\n" + "\n".join("  " + line if line else "" for line in details.splitlines())
    if expanded:
        return full_review_text(
            facts.get("security_check"), facts.get("professionalism_check"),
        )
    security = facts.get("security_check") or {}
    local = security.get("source") == "local_preflight"
    lines = [review_check_line(
        "Security check (local preflight)" if local else "Security check",
        security.get("local_status") if local else security.get("status"),
    )]
    status = security.get("local_status") if local else security.get("status")
    if status != "pass" and security.get("summary"):
        lines.append(review_summary_text(str(security["summary"])))
    for row in security.get("checks") or []:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or row.get("key") or "Security check")
        count = int(row.get("finding_count") or 0)
        suffix = f" ({count} finding{'s' if count != 1 else ''})" if count else ""
        lines.append(review_check_line(label, row.get("status")) + suffix)
        if row.get("status") != "pass":
            lines.extend(str(detail)[:256] for detail in row.get("details") or [])
    lines.append(professionalism_review_text(
        facts.get("professionalism_check"), include_checks=False,
    ))
    return "\n".join(lines)


def full_review_text(
    security: dict[str, Any] | None,
    professionalism: dict[str, Any] | None,
) -> str:
    """Render both checklists with labels, statuses, counts, and bounded detail."""

    sections = [
        _checklist_text(
            "Security check",
            security,
            labels={},
            note="These checks are not a security certification.",
        ),
        professionalism_review_text(professionalism),
    ]
    return "\n\n".join(sections)


def professionalism_review_text(
    check: dict[str, Any] | None, *,
    include_checks: bool = True,
) -> str:
    if (check or {}).get("status") in {"pass", "advisory"}:
        return review_text(check, include_checks=include_checks)
    return _checklist_text(
        "Professionalism check (agent-assessed, advisory)", check,
        labels=CHECK_LABELS, include_checks=include_checks,
    )


def _checklist_text(
    title: str,
    check: dict[str, Any] | None,
    *,
    labels: dict[str, str],
    note: str | None = None,
    include_checks: bool = True,
) -> str:
    value = check or {}
    lines = [review_check_line(title, value.get("status"))]
    summary = value.get("summary")
    if isinstance(summary, str) and summary.strip():
        lines.append(review_summary_text(summary))
    rows = value.get("checks") if include_checks else None
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "")
            label = str(row.get("label") or labels.get(key) or key.replace("_", " ").title())
            count = int(row.get("finding_count") or 0)
            suffix = f" ({count} finding{'s' if count != 1 else ''})" if count else ""
            lines.append(review_check_line(label, row.get("status")) + suffix)
            for detail in row.get("details") or []:
                lines.append(f"  {str(detail)[:256]}")
    if note:
        lines.append(note)
    return "\n".join(lines)
