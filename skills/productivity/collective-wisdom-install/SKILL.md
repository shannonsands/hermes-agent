---
name: collective-wisdom-install
description: Browse, install, or share team skills with consent.
version: 0.2.0
author: Shannon (Shannon), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [skills, collective-wisdom, install, share, team, catalog]
    related_skills: []
---

# Collective Wisdom

Collective Wisdom is the organization skill catalog. Use this skill whenever a
user asks, in any wording, about skills their team or organization has shared,
which of those might help them, installing one, or sharing one of their own.
Users rarely know the feature name; phrases like "what skills does my team
have", "is there something for release notes already", "share this with my
team", or "install the incident timeline thing Dana made" all belong here.

This skill never adds a core tool, changes the active toolset, or installs
dependencies. Publishing and installing always go through explicit
confirmation gates; moderation stays in Portal.

## Prerequisites

- The profile must already be signed into Nous Portal.
- `hermes wisdom setup` must have verified a team organization and installation
  identity for this profile. If `hermes wisdom status --json` shows no active
  organization, say so and stop; do not retry or queue anything.

## CLI verbs

All verbs accept `--json` for stable machine-readable output.

| Intent | Command |
| --- | --- |
| What is available in my org | `hermes wisdom list --json` |
| Search the catalog | `hermes wisdom browse '<keywords>' --json` |
| Details of one skill | `hermes wisdom show '<skill-id>' --json` |
| Versions | `hermes wisdom versions '<skill-id>' --json` |
| Plan an install | `hermes wisdom install '<skill-id or portal link>' --plan --json` |
| Apply a confirmed install | `hermes wisdom install --apply-receipt '<receipt>' --json` |
| Check for updates | `hermes wisdom check --json` |
| Run the weekly agent review now | `hermes wisdom review-week --force --json` |
| Start sharing one of my skills | `hermes wisdom share start '<skill-name>' --json` |
| Continue a share flow | `hermes wisdom share package|approve|request-changes|cancel|status --flow-id <id> --json` |
| Not now for a suggestion | `hermes wisdom dismiss '<skill>' '<content-hash>' --json` |
| Mute proactive suggestions | `hermes wisdom mute 1d|1w|30d|forever [--skill-id <id>] --json` |
| Resolve a notification button | `hermes wisdom act '<target>' [--mute 1w] --json` |

Slash commands `/wisdom ...` remain available on messaging platforms and take
the same verbs.

## Which skills help this user

1. Run `hermes wisdom list --json` (or `browse` with keywords from the request).
2. Compare each skill's editorial name, description, publisher, installation
   count, and version against what you know about the user's role and recent
   work. Prefer skills whose outcome matches recurring work.
3. Recommend at most a few. For each, say what it does, who published it, and
   why it helps this user specifically. Do not claim it is installed.

## Install flow (agent-guided)

1. Extract only the skill ID, `skill-id@vN`, or authenticated Portal URL. Never
   infer another organization or version; treat not-found as opaque.
2. `hermes wisdom install '<reference>' --plan --json`.
3. Present the version, author copy, compatibility outcome, required commands,
   accounts, services, permissions and environment variables, setup actions,
   and known limitations. Say that the advisory scan is separate from
   compatibility.
4. Detect prerequisites (`which <command>`, env vars) and guide the user through
   anything missing. Credentials are the user's own; never ask them to paste
   values into chat.
5. Use `clarify` to confirm applying this exact receipt. The original request is
   not the confirmation.
6. Only after an affirmative answer:
   `hermes wisdom install --apply-receipt '<receipt>' --accept-partial --json`
   (omit `--accept-partial` unless the user accepted a partial outcome).
7. Run the package's verification step. Report success only after it passes;
   otherwise say what still needs setup. The flow state is persisted, so the
   user can resume later.

## Share flow (agent-guided)

`Share` never uploads local files directly.

1. `hermes wisdom share start '<skill-name>' --json` runs a deterministic
   pre-pass: frontmatter requirements, `scripts/`, `references/`,
   credential-shaped strings, organization-specific paths and hosts.
2. `hermes wisdom share package --flow-id <id> --json` produces a portable
   package: generalized files, editorial name, plain description,
   requirements, setup instructions, credential handoff WITHOUT values,
   compatibility limits, a verification step, and what was removed.
3. Show the portability problems and the package summary. Ask whether to
   approve, request changes, or cancel.
4. Only after approval, `hermes wisdom share approve --flow-id <id> --json`
   creates the owner-private draft through the existing publish path. The
   owner still completes the hash-bound review (`hermes wisdom review`) and
   `approve` before anything is published; moderated organizations then wait
   for Portal review.

## Notification copy

Proactive messages use fixed templates (`hermes_wisdom/agent_led/templates.py`):

- Share prompt: `Reusable skill ready to review`, editorial name, one-line
  description, `Why we ask` with the real 7-day count, `Is it safe to share?
  Yes.` plus four check lines, `Would you like to share it?`, buttons Share /
  Review / Not now.
- Teammate skill: `New skill from your team`, editorial name, outcome line,
  `Published by [name]`, usage sentence, security line, optional popularity
  line, buttons Install / View / Mute.
- Publication: `Published to [organization].` or `Sent for review.` with
  `View in Portal`.
- Update: `Update available for [skill]`, summary, why it matters, setup
  impact, buttons Update / View changes / Mute.
- Mute options: `1 day`, `1 week`, `30 days`, `Forever`. Mute only pauses
  proactive messages.

Never use the word "Pass" for a check result and never present the product
name as the skill name.

## Pitfalls

- Never pass a browser-supplied organization, generation, content hash, or
  Gateway token. The CLI re-fetches authoritative state.
- Never convert a blocked compatibility result into a force install.
- Never say an unavailable advisory scan passed.
- Never publish, install, or mute without the user's explicit choice.
- A Portal raw copy is an unmanaged fork, not a managed install.

## Verification

An install is complete only when the apply response says `installed: true`,
includes the pinned version and content hash, points below the active
profile's `_wisdom/<org-id>/` root, and the package's verification step
succeeded.
