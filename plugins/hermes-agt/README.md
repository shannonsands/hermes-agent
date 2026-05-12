# hermes-agt — Microsoft Agent Governance Toolkit integration for Hermes

PoC plugin that wires Hermes Agent into Microsoft AGT
(`agent-governance-toolkit`) for runtime policy enforcement, audit, and trust
scoring. Sandboxing is **not** in scope for this PoC; that's a Phase 2 story
on top of OpenShell / NemoClaw.

## What it does

- Runs every Hermes tool call through `agent_os.policies.PolicyEvaluator`
  before dispatch (sub-millisecond, deterministic).
- Blocks denied calls via the standard Hermes `pre_tool_call` block-message
  contract — the tool returns a structured error and the agent gets the
  rejection in its tool-result stream.
- Reuses Hermes's existing approval UX (CLI prompt, gateway buttons,
  smart-mode auxiliary LLM) when the policy says human review is required.
- Dual-writes every decision to:
  - AGT's Merkle-chained `agentmesh.governance.audit.AuditLog`
    (so `agt verify --evidence` works).
  - Hermes's `agent.log` (so existing log tooling keeps working).
- Tracks per-session trust scores; low trust forces human review on
  otherwise-allowed actions.

## Install

```bash
# 1. Install AGT into your Hermes venv. The Hermes-installed venv has pip
#    stripped, but uv can target it directly:

# Windows (note: HERMES_HOME = AppData\Local\hermes, NOT ~/.hermes)
HERMES_PY="$LOCALAPPDATA/hermes/hermes-agent/venv/Scripts/python.exe"
# Linux/macOS
# HERMES_PY="$HOME/.hermes/hermes-agent/venv/bin/python"

uv pip install --python "$HERMES_PY" "agent-governance-toolkit[full]"

# 2. Enable the plugin
hermes plugins enable hermes-agt

# 3. (Optional) Drop a custom policy file. The plugin ships a sane default
#    that mirrors Hermes's built-in DANGEROUS_PATTERNS, so this step is
#    only needed if you want to override or extend.
mkdir -p "$(dirname $(hermes config path))/policies"
# (the plugin auto-falls-back to its bundled policies/default.yaml)

# 4. Restart your session
```

## Configuration

`~/.hermes/config.yaml`:

```yaml
hermes_agt:
  enabled: true                          # default: true when plugin loaded
  policy_dir: ~/.hermes/policies         # where to load *.yaml policies from
  default_action: allow                  # allow | deny
  audit_path: ~/.hermes/agt-audit.jsonl  # AGT AuditLog sink
  trust:
    enabled: true
    threshold: 0.5                       # below this, force human review
    deny_penalty: 0.15                   # trust delta on each denial
  fail_open: true                        # if AGT errors, allow the call (PoC)
```

When `fail_open: false`, any AGT error becomes a deny — safer but
crashier. Default is `true` for PoC; flip to `false` for production.

## How it interacts with `approvals.mode`

| `approvals.mode` | AGT verdict | Behavior |
|---|---|---|
| any | ALLOW (no human-review flag) | run tool |
| any | DENY / BLOCK | reject, audit |
| any | ALLOW + `require_human_approval_on` match | hand off to Hermes approval UX |
| `manual` | (legacy fallback only) | unchanged Hermes approval prompt |
| `smart` | AGT runs first; aux LLM consulted only on review | strictly faster than smart-mode alone |
| `off` / `--yolo` | DENY still rejects | yolo bypasses prompts, not policy |

## Status

PoC. Pending official Microsoft partnership. See `~/.hermes/plans/2026-05-12_010436-hermes-agt-poc.md` for the full plan.

## Known limitations / dogfooding findings

1. **Outer-invocation matches.** When Hermes runs a shell command that
   itself invokes `hermes chat -q "...prompt text..."`, AGT evaluates
   against the literal command string — including the prompt content.
   So a prompt mentioning "chmod 777" will match the
   `chmod-world-writable` rule even if the inner agent never actually
   runs that command. Mitigation: prompts that discuss dangerous
   patterns should be passed via stdin or files instead of `-q "..."`.
   A future fix could special-case `hermes chat -q "..."` and let the
   inner session's own AGT policy do the real evaluation.

2. **Trust state shares a "default" bucket on the CLI.** When
   `session_id` is empty (which happens for `hermes chat -q` runs
   before the session id is allocated), all CLI invocations share one
   trust score. Persistent denies across CLI invocations can therefore
   accumulate. In interactive sessions trust is properly keyed by
   session.

3. **`requires_review` not yet wired to Hermes approval UX.** The
   interceptor records when trust falls below threshold, but doesn't
   currently force the call into Hermes's approval prompt. Phase C.9
   write-up calls this out as next.
