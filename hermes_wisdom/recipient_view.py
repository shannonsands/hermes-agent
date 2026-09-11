"""Concise recipient offers; exact consent and review remain authoritative."""
from gateway.wisdom_command import WisdomAction, WisdomItem
from .review_presentation import review_card_text, sharing_checks_passed


def recommendation_item(interaction, advice, *, expanded=False):
    facts = interaction["facts"]
    identity = interaction["id"]
    unavailable = advice.get("assessment_status") == "unavailable"
    detail = advice["explanation"]
    if expanded:
        detail += "\n\nPackage facts: " + str(facts.get("slug") or facts.get("skill_id") or "Skill")
        if facts.get("version"):
            detail += f" · v{facts['version']}"
        if "update_mode" in facts:
            mode = {None: "Organization default", "MANUAL": "Manual", "AUTO_WITH_NOTICE": "Automatic with notice", "REQUIRED": "Required"}.get(facts["update_mode"], "Unavailable")
            detail += f"\nFuture updates: {mode}"
    compatibility = facts.get("compatibility") or {}
    if compatibility and (expanded or compatibility.get("outcome") != "compatible"):
        detail += "\nCompatibility: " + str(compatibility.get("outcome") or "unavailable")
    if facts.get("modified"):
        detail += "\nLocal changes require full review."
    if facts.get("sensitive_expansion"):
        detail += "\nAdditional permissions or requirements need separate approval."
    if expanded or not sharing_checks_passed(facts):
        detail += "\n\n" + review_card_text(facts, expanded=expanded, current=not unavailable)
    detail += "\n\nWould you like to install this skill?"
    allowed = interaction["actions"] if interaction.get("state", "pending") == "pending" and not interaction.get("deferred") else []
    actions = []
    if "inspect" in allowed:
        actions.append(WisdomAction("View Details", callback_data=f"wi:agent:assessment.show:{identity}"))
    if "defer" in allowed:
        actions.append(WisdomAction("Not Now", callback_data=f"wi:agent:defer:{identity}"))
    if allowed:
        actions.append(WisdomAction("Mute Skill Recommendations", "mute", local_command="/wisdom mute"))
    if "confirm" in allowed and not unavailable:
        actions.append(WisdomAction("Install Skill", callback_data=f"wi:agent:confirm:{identity}", primary=True))
    return WisdomItem(title=advice["title"], detail=detail, actions=actions)


def recommendation_summary(operation, organization_name=None):
    if operation == "update":
        return "A shared skill has an update that could be useful to you!"
    organization = "your organization"
    if isinstance(organization_name, str) and organization_name.strip():
        organization += f" ({organization_name.strip()})"
    return f"Hermes has detected a newly shared skill in {organization}, that could be useful to you!"
