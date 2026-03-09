"""DevOps AI agent -- OpenAI-compatible, streaming, tool-use loop."""
from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from .config import get_settings
from .tools.registry import TOOLS, execute_tool

DEV_PROMPT = """IMPORTANT: Never use emoji, emoticons, or unicode symbols in any response. Use plain ASCII text only.

You are a friendly DevOps assistant helping developers onboard their app for the first time. After this one-time setup, all future deployments happen automatically via CI/CD. Your job: discover the repo, ask a few targeted questions, then hand off to the DevOps team.

## Who you're talking to
Developers who know their app but not the infrastructure. Speak plainly. No Kubernetes jargon unless they ask.

## Onboarding workflow

**Step 0 — Discover the repo first.**
Call `discover_repo` with the github_repo — silently, no output yet.
This validates the repo exists and reveals: language/stack, Dockerfile, exposed port, env vars needed.
If discovery fails (repo not found, private with no access, no Dockerfile), tell the developer and stop — do not create a project.

**Step 1 — Register the project.**
Only after successful discovery, call `create_project`:
- `name`: lowercase slug from the repo name (e.g. "sre-agent" from "cloudomate/sre-agent")
- `github_repo`: owner/repo (e.g. "cloudomate/sre-agent")

**Step 2 — Show the onboarding form. DO NOT ask questions in text.**
CRITICAL: After discovery + create_project succeed, you MUST output a discover-form block and NOTHING ELSE in that message. Do not list questions. Do not use numbered lists. Do not use emoji. Do not ask anything in text. The form is the only output. The UI will render it as an interactive form.

Output ONLY this (fill in values from discovery, valid JSON):
```discover-form
{"detected_stack":"<stack>","detected_port":<port>,"has_dockerfile":<true|false>,"health_path":"<path or null>","required_env_vars":["VAR1","VAR2"],"suggested_environments":["staging","prod"],"suggested_registry":""}
```

Wait silently for the developer to submit the form.

**Step 3 — On `[ONBOARDING FORM RESPONSE]`.**
The developer has filled in the form (submitted as YAML). Parse environments, domains, registry, and env_vars from the YAML block.
Then show a brief confirmation summary and output a deploy-confirm block:

```deploy-confirm
{"project":"<slug>","environment":"<envs joined>","image":"<registry>/<project>:latest","domain":"<primary domain>","summary_points":["Stack: <stack>","Environments: <envs>","CI/CD: auto-deploy on push to main"]}
```

**Step 4 — On `__SUBMIT_CONFIRMED__`.**
Call `submit_deployment_request`. No more questions.
After submission: "Submitted. A DevOps engineer will review and set up everything — you'll get a CI/CD workflow to commit once it's done."

## After first deployment
- Future deploys: automatic via CI/CD (GitHub Actions). No need to come back here.
- Status/logs: `get_deployment_status`, `get_logs`, `run_health_check`.

## Rules
- NEVER fabricate output or status.
- NEVER submit without confirmation.
"""

DEVOPS_PROMPT = """IMPORTANT: Never use emoji, emoticons, or unicode symbols in any response. Use plain ASCII text only.

You are a senior DevOps/DevSecOps engineer assistant. The cluster and infrastructure are already running. Your job is to plan each app deployment onto that existing infra, get explicit sign-off from the DevOps engineer, then execute.

## GitOps structure
- `charts/app/` — shared Helm chart for ALL projects (already on cluster via ArgoCD)
- `projects/<project>/values-<env>.yaml` — per-project, per-env values
- `argocd/<project>-<env>.yaml` — ArgoCD Application CR, one per env

---

## Onboarding workflow

### Step 1 — Discover the repo first (silently, no output yet)
Call `discover_repo` with the github_repo.
If discovery fails (repo not found or inaccessible), report the error and stop — do not create a project.
On success, learn: language, framework, container port, health check path, env vars the app reads.
Then call `create_project` (idempotent) to register it.

### Step 2 — Ask deployment planning questions (one message, all at once)
Ask the DevOps engineer what's needed to plan the deployment. Skip anything discovery already answered.

- Which environments? (e.g. staging, prod)
- Domain/hostname per environment?
- Container registry? (e.g. cr.imys.in/hci)
- Replicas? Enable autoscaling (HPA)? Min/max? (default: 1 replica, no HPA)
- CPU/memory limits? (defaults: requests 100m/128Mi · limits 500m/512Mi)
- Secret names the app needs? (just key names — DevOps injects actual values into k8s Secret separately, never in GitOps)
- Any plain env vars to set (non-secret)?
- Run security scan before deploying to prod?

### Step 3 — Present the deployment plan and WAIT for approval
Show exactly what will be pushed. Do NOT touch GitOps yet.

```
=== DEPLOYMENT PLAN: <project> ===
Repo:    <github_repo>
Stack:   <language> · port <N> · health <path>

STAGING
  Image:     <registry>/<project>:latest
  Domain:    <hostname>
  Replicas:  <N>  (HPA: <yes/no>)
  Resources: req 100m/128Mi  |  limits 500m/512Mi
  Env vars:  KEY=value, ...
  Secrets:   KEY1, KEY2  (injected via k8s Secret, not stored in GitOps)

PROD
  (same format)

Files to create in GitOps repo:
  ✦ charts/app/                            (shared chart — skip if exists)
  ✦ projects/<project>/values-staging.yaml
  ✦ projects/<project>/values-prod.yaml
  ✦ argocd/<project>-staging.yaml
  ✦ argocd/<project>-prod.yaml

CI/CD after first deploy:
  GitHub Actions → build image → push to registry → update image tag in values → ArgoCD auto-syncs

→ Does this look correct? Reply APPROVE to proceed, or tell me what to change.
```

### Step 4 — Execute (only after explicit APPROVE)
Run in this order:
1. `pre_deploy_security_check` — prod only
2. `push_shared_chart` — idempotent, skips if `charts/app/` already exists
3. `push_project_values` — write `projects/<project>/values-<env>.yaml` for each env with full config (image, port, domain, replicas, resources, autoscaling, env_vars, secret key names, health_path)
4. `register_argocd_app` — write `argocd/<project>-<env>.yaml` and trigger sync, once per env
5. `approve_deployment_request` — only if this came from a developer submission
6. `post_deploy_security_check`
7. `generate_cicd_workflow` — produce the GitHub Actions YAML

### Step 5 — Hand off CI/CD to the developer
Return the workflow YAML and instruct:
- Commit it as `.github/workflows/deploy.yml`
- Add GitHub secrets: `REGISTRY_USER`, `REGISTRY_PASSWORD`, `GITOPS_TOKEN` (PAT with write access to GitOps repo)
- From here on: push to main → CI builds & pushes image → updates `image.tag` in values file → ArgoCD deploys. No more manual submissions.

---

## One-time cluster/environment registration
When DevOps wants to register a cluster or environment config, call `upsert_environment`:
`namespace`, `deployment`, `platform` (eks/aks/gke/self-hosted), `ingress_class`, `domain`, `tls`, `cert_manager_issuer`, `cert_manager_challenge`, `lb_type`, `lb_annotations`, `acm_certificate_arn`, `external_dns`.
Then call `infra_setup_guide` for commands.

---

Be precise and technical. Use kubectl/docker/helm terminology freely.
Security: `pre_deploy_security_check` before prod, `post_deploy_security_check` after, `security_review_repo` on request.
NEVER push to GitOps or execute a deployment without explicit human approval.
NEVER fabricate output, logs, or status.
"""

# Tools that developer-persona users cannot call
DEV_ONLY_BLOCKED = {
    "upsert_environment", "delete_environment",
    "update_project", "delete_project",
    "deploy", "rollback",  # developers must use submit_deployment_request
    "approve_deployment_request", "reject_deployment_request",  # devops/admin only
}


_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U00002600-\U000027BF"  # misc symbols (☀✅⚠ etc.)
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "\U00002702-\U000027B0"
    "]+",
    flags=re.UNICODE,
)

def _strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text)


def _make_client() -> AsyncOpenAI:
    settings = get_settings()
    return AsyncOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        timeout=90.0,  # fail fast instead of waiting for Cloudflare 524
    )


async def run_agent_streaming(
    messages: list[dict[str, Any]],
    persona: str = "developer",
    user: dict[str, Any] | None = None,
    project_names: list[str] | None = None,
    active_project: str | None = None,
) -> AsyncIterator[str | dict]:
    """
    Run the agent loop, yielding:
      - str: raw text delta (LLM output to display)
      - dict: structured event, one of:
          {"type": "tool_start", "name": str, "args": dict}
          {"type": "tool_done",  "name": str, "result": str}

    Handles tool calls automatically until the model has no more tools to call.
    """
    settings = get_settings()
    client = _make_client()

    # Choose system prompt and tool list based on persona
    if persona == "devops":
        system = DEVOPS_PROMPT
        tools = TOOLS
    else:
        system = DEV_PROMPT
        tools = [t for t in TOOLS if t["function"]["name"] not in DEV_ONLY_BLOCKED]

    # Inject user + project context into system prompt
    display = (user or {}).get("display_name") or (user or {}).get("username", "")
    if display:
        proj_list = ", ".join(project_names or []) or "none"
        system += f"\n\n---\nCurrent user: {display} (role={persona})\nAccessible projects: {proj_list}"
        if active_project:
            system += f"\nActive project (this chat is scoped to): {active_project} — treat this as the default project for all tool calls unless the user specifies otherwise."

    # Prepend system prompt if not already present
    if not messages or messages[0].get("role") != "system":
        messages = [{"role": "system", "content": system}] + messages

    while True:
        # Collect streamed text and tool calls
        text_chunks: list[str] = []
        tool_calls_raw: dict[int, dict] = {}  # index -> partial call

        stream = await client.chat.completions.create(
            model=settings.llm_model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            stream=True,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        finish_reason = None

        async for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if not choice:
                continue

            finish_reason = choice.finish_reason or finish_reason
            delta = choice.delta

            # Stream text to caller
            if delta.content:
                clean = _strip_emoji(delta.content)
                text_chunks.append(clean)
                if clean:
                    yield clean

            # Accumulate tool call fragments
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_raw:
                        tool_calls_raw[idx] = {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    entry = tool_calls_raw[idx]
                    if tc.id:
                        entry["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            entry["function"]["name"] += tc.function.name
                        if tc.function.arguments:
                            entry["function"]["arguments"] += tc.function.arguments

        # Append assistant message to history
        tool_calls_list = [tool_calls_raw[i] for i in sorted(tool_calls_raw)]
        assistant_msg: dict[str, Any] = {"role": "assistant"}
        if text_chunks:
            assistant_msg["content"] = "".join(text_chunks)
        if tool_calls_list:
            assistant_msg["tool_calls"] = tool_calls_list
        messages.append(assistant_msg)

        # Fallback: some local LLMs (e.g. Qwen) emit tool calls as text content
        # instead of proper delta.tool_calls.  Try to parse them out.
        if not tool_calls_list and text_chunks:
            tool_calls_list = _parse_text_tool_calls("".join(text_chunks))
            if tool_calls_list:
                # Rebuild assistant message with proper tool_calls (strip raw text)
                messages[-1] = {"role": "assistant", "tool_calls": tool_calls_list}

        # If no tool calls, we're done
        if not tool_calls_list:
            break

        # Execute tool calls and append results
        for tc in tool_calls_list:
            fn_name = tc["function"]["name"]
            try:
                fn_args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                fn_args = {}

            # Emit structured event so UI can show real-time tool activity
            yield {"type": "tool_start", "name": fn_name, "args": fn_args}

            result = execute_tool(fn_name, fn_args, user=user)

            yield {"type": "tool_done", "name": fn_name, "result": result}

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })


def _fmt_args(args: dict) -> str:
    return ", ".join(f"{k}={json.dumps(v)}" for k, v in args.items())


# Known tool names for fallback text parsing (populated lazily to avoid import ordering issues)
def _known_tools() -> set[str]:
    return {t["function"]["name"] for t in TOOLS}

# Qwen/Llama native tool-call patterns that appear as text content
_TOOL_CALL_XML_RE = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL
)
_TOOL_CALL_JSON_RE = re.compile(
    r'\{"name"\s*:\s*"([^"]+)"\s*,\s*"(?:arguments|parameters)"\s*:\s*(\{.*?\})\}',
    re.DOTALL,
)


def _parse_text_tool_calls(text: str) -> list[dict]:
    """Extract tool calls embedded as text by non-OpenAI-compliant LLMs."""
    calls = []

    # Pattern 1: <tool_call>{...}</tool_call>
    for m in _TOOL_CALL_XML_RE.finditer(text):
        try:
            obj = json.loads(m.group(1))
            name = obj.get("name") or obj.get("function")
            args = obj.get("arguments") or obj.get("parameters") or {}
            if isinstance(args, str):
                args = json.loads(args)
            if name in _known_tools():
                calls.append(_make_tc(name, args))
        except (json.JSONDecodeError, KeyError):
            pass

    if calls:
        return calls

    # Pattern 2: {"name": "...", "arguments": {...}}
    for m in _TOOL_CALL_JSON_RE.finditer(text):
        name = m.group(1)
        if name not in _known_tools():
            continue
        try:
            args = json.loads(m.group(2))
            calls.append(_make_tc(name, args))
        except json.JSONDecodeError:
            pass

    return calls


def _make_tc(name: str, args: dict) -> dict:
    return {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


async def handle_webhook_event(event_summary: str, project_hint: str = "") -> str:
    """Process a GitHub webhook event and return the agent's response."""
    user_content = f"[WEBHOOK] {event_summary}"
    if project_hint:
        user_content += f"\nProject hint: {project_hint}"

    messages = [{"role": "user", "content": user_content}]
    result_parts: list[str] = []
    async for chunk in run_agent_streaming(messages):
        result_parts.append(chunk)
    return "".join(result_parts)
