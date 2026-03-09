"""Repository discovery — clones repo locally and does deep analysis.

Public helpers re-exported for use in registry tools:
  clone_repo, all_files, find_files, grep_files, read_file, rel_path
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


# ─── Clone ────────────────────────────────────────────────────────────────────

def _clone(github_repo: str, branch: str, token: str | None) -> tuple[Path, str] | tuple[None, str]:
    """Shallow-clone the repo into a temp dir. Returns (path, branch) or (None, error)."""
    tmpdir = Path(tempfile.mkdtemp(prefix="devops-discovery-"))
    url = (
        f"https://{token}@github.com/{github_repo}.git"
        if token else
        f"https://github.com/{github_repo}.git"
    )
    for br in ([branch] if branch != "main" else ["main", "master"]):
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", br, url, str(tmpdir)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            return tmpdir, br
        # clean partial clone
        shutil.rmtree(tmpdir, ignore_errors=True)
        tmpdir = Path(tempfile.mkdtemp(prefix="devops-discovery-"))

    # try without --branch (default branch)
    result = subprocess.run(
        ["git", "clone", "--depth", "1", url, str(tmpdir)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode == 0:
        # detect actual branch
        r2 = subprocess.run(
            ["git", "-C", str(tmpdir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        )
        br = r2.stdout.strip() or "main"
        return tmpdir, br

    shutil.rmtree(tmpdir, ignore_errors=True)
    return None, result.stderr.strip()


# ─── File helpers ─────────────────────────────────────────────────────────────

def _all_files(root: Path) -> list[Path]:
    """Return all files, skipping .git and common noise dirs."""
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "vendor", "target", "dist", "build"}
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for f in filenames:
            results.append(Path(dirpath) / f)
    return results


def _rel(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


def _read(path: Path, max_bytes: int = 32_000) -> str:
    try:
        with open(path, "r", errors="replace") as f:
            return f.read(max_bytes)
    except Exception:
        return ""


def _find(files: list[Path], root: Path, *patterns: str) -> list[Path]:
    """Find files matching any of the glob patterns (relative path)."""
    results = []
    for f in files:
        rel = _rel(f, root)
        if any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(f.name, p) for p in patterns):
            results.append(f)
    return results


def _grep(files: list[Path], pattern: str, include: str = "*", max_results: int = 20) -> list[tuple[Path, int, str]]:
    """Search for a regex pattern across files matching include glob."""
    rx = re.compile(pattern, re.IGNORECASE)
    hits: list[tuple[Path, int, str]] = []
    for f in files:
        if not fnmatch.fnmatch(f.name, include):
            continue
        try:
            for i, line in enumerate(open(f, errors="replace"), 1):
                if rx.search(line):
                    hits.append((f, i, line.rstrip()))
                    if len(hits) >= max_results:
                        return hits
        except Exception:
            pass
    return hits


# ─── Parsers ──────────────────────────────────────────────────────────────────

def _parse_dockerfile(content: str) -> dict[str, Any]:
    info: dict[str, Any] = {}
    for line in content.splitlines():
        s = line.strip()
        up = s.upper()
        if up.startswith("FROM ") and "base_image" not in info:
            info["base_image"] = s.split()[1]
        elif up.startswith("EXPOSE "):
            info.setdefault("ports", []).extend(s.split()[1:])
        elif up.startswith("USER ") and s.split()[1] not in ("root", "0"):
            info["non_root_user"] = s.split()[1]
        elif up.startswith("HEALTHCHECK "):
            info["has_healthcheck"] = True
        elif up.startswith("CMD ") or up.startswith("ENTRYPOINT "):
            info["entrypoint"] = s
        elif up.startswith("ENV "):
            parts = s[4:].split("=", 1)
            if len(parts) == 2:
                info.setdefault("env_vars", []).append(parts[0].strip())
    if "non_root_user" not in info:
        info["runs_as_root"] = True
    return info


def _parse_compose(content: str) -> list[dict]:
    """Extract services with ports, env vars, volumes."""
    services: list[dict] = []
    cur: dict | None = None
    section = None
    for line in content.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if indent == 2 and stripped.endswith(":") and not stripped.startswith("-"):
            name = stripped[:-1]
            if name not in ("version", "volumes", "networks", "services", "configs", "secrets"):
                cur = {"name": name, "ports": [], "env_vars": [], "depends_on": [], "volumes": []}
                services.append(cur)
                section = None
        elif cur and indent == 4 and stripped.endswith(":"):
            section = stripped[:-1]
        elif cur and indent >= 6 and stripped.startswith("- "):
            val = stripped[2:].strip()
            if section == "ports":
                cur["ports"].append(val)
            elif section in ("environment",):
                cur["env_vars"].append(val.split("=")[0].split(":")[0])
            elif section == "depends_on":
                cur["depends_on"].append(val)
            elif section == "volumes":
                cur["volumes"].append(val)
        elif cur and indent == 4 and "=" in stripped and section == "environment":
            cur["env_vars"].append(stripped.split("=")[0])
    return services


def _detect_language(files: list[Path], root: Path) -> list[str]:
    names = {f.name for f in files}
    exts = {f.suffix for f in files}
    langs = []
    checks = [
        ({"go.mod"}, ".go", "Go"),
        ({"Cargo.toml"}, ".rs", "Rust"),
        ({"package.json"}, ".ts", "TypeScript/Node.js"),
        ({"package.json"}, ".js", "JavaScript/Node.js"),
        ({"requirements.txt", "pyproject.toml", "setup.py", "Pipfile"}, ".py", "Python"),
        ({"pom.xml", "build.gradle", "build.gradle.kts"}, ".java", "Java/Kotlin"),
        ({"Gemfile"}, ".rb", "Ruby"),
        ({"composer.json"}, ".php", "PHP"),
        (set(), ".cs", "C#/.NET"),
        (set(), ".cpp", "C++"),
    ]
    seen = set()
    for markers, ext, lang in checks:
        if lang in seen:
            continue
        if (markers & names) or ext in exts:
            langs.append(lang)
            seen.add(lang)
    return langs or ["Unknown"]


# ─── Main ─────────────────────────────────────────────────────────────────────

# ─── Public aliases for registry tools ───────────────────────────────────────

def clone_repo(github_repo: str, branch: str = "main", token: str | None = None) -> tuple[Path, str] | tuple[None, str]:
    """Public alias for _clone — returns (path, branch) or (None, error)."""
    return _clone(github_repo, branch, token)


all_files = _all_files
find_files = _find
grep_files = _grep
read_file = _read
rel_path = _rel


# ─── Discover (one-shot) ──────────────────────────────────────────────────────

def discover_repo(
    github_repo: str,
    branch: str = "main",
    github_token: str | None = None,
) -> str:
    tmpdir, br_or_err = _clone(github_repo, branch, github_token)
    if tmpdir is None:
        return f"Could not clone {github_repo}: {br_or_err}"

    try:
        return _analyse(tmpdir, github_repo, br_or_err)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _analyse(root: Path, github_repo: str, branch: str) -> str:
    files = _all_files(root)
    rel_files = [_rel(f, root) for f in files]
    sections: list[str] = [f"## Repository Analysis: {github_repo} ({branch})\n"]

    # ── README ────────────────────────────────────────────────────────────────
    readme = next((f for f in files if f.name.lower() in ("readme.md", "readme.rst", "readme.txt")), None)
    if readme:
        content = _read(readme, 3000)
        # Extract first paragraph as summary
        paras = [p.strip() for p in re.split(r'\n{2,}', content) if p.strip() and not p.startswith('#')]
        summary = paras[0][:400] if paras else ""
        if summary:
            sections.append(f"**Summary:** {summary}\n")

    # ── Language / stack ──────────────────────────────────────────────────────
    langs = _detect_language(files, root)
    sections.append(f"**Stack:** {', '.join(langs)}")
    sections.append(f"**Files:** {len(files)} total")

    # ── Dockerfile(s) ─────────────────────────────────────────────────────────
    dockerfiles = _find(files, root, "Dockerfile", "Dockerfile.*", "*.dockerfile")
    if dockerfiles:
        for df in dockerfiles[:3]:
            content = _read(df)
            info = _parse_dockerfile(content)
            rel = _rel(df, root)
            sections.append(f"\n**Dockerfile:** `{rel}`")
            if info.get("base_image"):
                sections.append(f"  - Base image: `{info['base_image']}`")
            if info.get("ports"):
                sections.append(f"  - Exposed port(s): {', '.join(info['ports'])}")
            if info.get("runs_as_root"):
                sections.append("  - ⚠️  Runs as root (no USER directive)")
            if info.get("non_root_user"):
                sections.append(f"  - ✅ Non-root user: `{info['non_root_user']}`")
            if info.get("has_healthcheck"):
                sections.append("  - ✅ HEALTHCHECK defined")
            else:
                sections.append("  - ⚠️  No HEALTHCHECK")
            if info.get("env_vars"):
                sections.append(f"  - ENV vars set: {', '.join('`' + v + '`' for v in info['env_vars'][:8])}")
    else:
        sections.append("\n**Dockerfile:** not found")

    # ── Docker Compose ────────────────────────────────────────────────────────
    compose_files = _find(files, root, "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
    if compose_files:
        cf = compose_files[0]
        services = _parse_compose(_read(cf))
        sections.append(f"\n**Docker Compose:** `{_rel(cf, root)}`")
        for svc in services:
            line = f"  - Service `{svc['name']}`"
            if svc["ports"]:
                line += f" — ports: {', '.join(svc['ports'])}"
            if svc["depends_on"]:
                line += f" — depends on: {', '.join(svc['depends_on'])}"
            sections.append(line)
            if svc["env_vars"]:
                sections.append(f"    Env vars: {', '.join(svc['env_vars'][:10])}")
    else:
        sections.append("\n**Docker Compose:** not found")

    # ── Kubernetes / Helm ─────────────────────────────────────────────────────
    k8s_files = [f for f in files if any(
        _rel(f, root).startswith(d) for d in ("k8s/", "kubernetes/", ".k8s/", "deploy/", "charts/", "helm/")
    ) and f.suffix in (".yaml", ".yml")]
    helm_chart = next((f for f in files if f.name == "Chart.yaml"), None)

    if helm_chart or k8s_files:
        sections.append("\n**Kubernetes/Helm:**")
        if helm_chart:
            sections.append(f"  - Helm chart: `{_rel(helm_chart, root)}`")
        if k8s_files:
            dirs = set(_rel(f, root).split("/")[0] for f in k8s_files)
            sections.append(f"  - Manifests: {len(k8s_files)} file(s) in {', '.join(dirs)}/")
            # Peek at resource kinds
            kinds = set()
            for f in k8s_files[:10]:
                for m in re.finditer(r'^kind:\s*(\w+)', _read(f), re.MULTILINE):
                    kinds.add(m.group(1))
            if kinds:
                sections.append(f"  - Resource kinds: {', '.join(sorted(kinds))}")
    else:
        sections.append("\n**Kubernetes/Helm:** no manifests found")

    # ── CI/CD ─────────────────────────────────────────────────────────────────
    gha = [f for f in files if _rel(f, root).startswith(".github/workflows/") and f.suffix in (".yml", ".yaml")]
    if gha:
        names = [f.name for f in gha[:5]]
        sections.append(f"\n**CI/CD:** GitHub Actions — {', '.join(names)}")
        # Look for docker build/push steps
        for f in gha:
            content = _read(f)
            if "docker/build-push-action" in content or "docker build" in content.lower():
                sections.append(f"  - `{f.name}` builds & pushes Docker image")
            if "helm" in content.lower() and "deploy" in content.lower():
                sections.append(f"  - `{f.name}` does Helm deploy")
    else:
        sections.append("\n**CI/CD:** no GitHub Actions workflows found")

    # ── Env vars ──────────────────────────────────────────────────────────────
    env_example = next((f for f in files if f.name in (".env.example", ".env.sample", ".env.template")), None)
    required_env: list[str] = []
    if env_example:
        for line in _read(env_example).splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=', line)
                if m:
                    required_env.append(m.group(1))
        sections.append(f"\n**Env vars** (from `{_rel(env_example, root)}`):")
        sections.append("  " + ", ".join(f"`{k}`" for k in required_env) if required_env else "  (none)")
    else:
        # Grep source for os.getenv / process.env / os.environ patterns
        hits = _grep(files, r'os\.getenv\(|process\.env\.|os\.environ\[|System\.getenv\(', max_results=30)
        env_names = []
        for _, _, line in hits:
            for m in re.finditer(r'(?:os\.getenv|os\.environ\[|process\.env\.|System\.getenv)\(["\']?([A-Z_][A-Z0-9_]+)', line):
                env_names.append(m.group(1))
        env_names = sorted(set(env_names))
        if env_names:
            sections.append(f"\n**Env vars** (grepped from source):")
            sections.append("  " + ", ".join(f"`{k}`" for k in env_names[:20]))
        else:
            sections.append("\n**Env vars:** none detected")

    # ── Exposed ports (grep fallback) ─────────────────────────────────────────
    if not dockerfiles:
        port_hits = _grep(files, r'(?:PORT|port|listen|bind).*(?:8\d{3}|3000|5000|4000)', max_results=5)
        if port_hits:
            ports = set()
            for _, _, line in port_hits:
                for m in re.finditer(r'\b([3-9]\d{3}|[1-5]\d{4})\b', line):
                    ports.add(m.group(1))
            if ports:
                sections.append(f"\n**Detected port(s):** {', '.join(sorted(ports))}")

    # ── Deployment recommendation ──────────────────────────────────────────────
    sections.append("\n---\n**Deployment options:**")
    opts = []
    if compose_files:
        opts.append("1. **Docker Compose** — config already present")
    if dockerfiles:
        opts.append(f"{'2' if opts else '1'}. **Docker on VM** (SSH) — Dockerfile ready")
    if helm_chart or k8s_files:
        opts.append(f"{'3' if len(opts)>=2 else len(opts)+1}. **Kubernetes/Helm** — manifests present")
    if not opts:
        opts.append("No deployment config found — need to generate one")
    sections.extend(opts)

    # ── Database / backing services detection ─────────────────────────────────
    _DB_PATTERNS = {
        "PostgreSQL": re.compile(r'postgres|psycopg|pg\b|POSTGRES|DATABASE_URL.*postgres', re.I),
        "MySQL":      re.compile(r'mysql|pymysql|mysqlclient|MYSQL_', re.I),
        "MongoDB":    re.compile(r'mongo|pymongo|MONGO_', re.I),
        "Redis":      re.compile(r'redis|REDIS_', re.I),
        "SQLite":     re.compile(r'sqlite|\.db\b', re.I),
        "Elasticsearch": re.compile(r'elasticsearch|ELASTIC_', re.I),
        "RabbitMQ":   re.compile(r'rabbitmq|amqp|pika\b', re.I),
        "Kafka":      re.compile(r'kafka|confluent', re.I),
    }
    detected_dbs: list[str] = []
    _search_content = "\n".join(
        _read(f, 2000) for f in files
        if f.suffix in (".py", ".js", ".ts", ".go", ".rb", ".java", ".toml", ".txt", ".json")
        and "node_modules" not in str(f) and ".venv" not in str(f)
    )
    for db_name, pat in _DB_PATTERNS.items():
        if pat.search(_search_content):
            detected_dbs.append(db_name)

    # ── Secret env vars ────────────────────────────────────────────────────────
    _SECRET_KEY_PAT = re.compile(
        r'(?:PASSWORD|SECRET|_KEY|_TOKEN|API_KEY|PRIVATE_KEY|ACCESS_KEY|AUTH_|CREDENTIAL)',
        re.I,
    )
    secret_vars = [k for k in required_env if _SECRET_KEY_PAT.search(k)]
    non_secret_vars = [k for k in required_env if k not in secret_vars]

    # ── What we already know (auto-answered) ──────────────────────────────────
    sections.append("\n---\n**Discovered automatically:**")

    # Infra hint
    if helm_chart or k8s_files:
        infra_hint = "Kubernetes (manifests already in repo)"
    elif compose_files:
        infra_hint = "Docker Compose (config already in repo)"
    elif dockerfiles:
        infra_hint = "Docker (Dockerfile present)"
    else:
        infra_hint = None
    if infra_hint:
        sections.append(f"  - **Target infra:** {infra_hint}")

    # Databases
    if detected_dbs:
        sections.append(f"  - **Databases / services:** {', '.join(detected_dbs)}")

    # Image tag
    if dockerfiles:
        sections.append("  - **Image tag:** will use `latest` for first deployment (override if needed)")

    # Deploy trigger
    if gha:
        sections.append("  - **Deploy trigger:** GitHub Actions already present — will hook into existing pipeline")
    else:
        sections.append("  - **Deploy trigger:** will set up auto-deploy on push to `main` via CI/CD after first deployment")

    # Non-secret env vars
    if non_secret_vars:
        sections.append(f"  - **Config env vars:** {', '.join(f'`{k}`' for k in non_secret_vars[:10])}{'...' if len(non_secret_vars) > 10 else ''}")

    # ── Database connection fields needed ─────────────────────────────────────
    # For each detected DB, define what connection info is needed
    _DB_FIELDS: dict[str, dict] = {
        "PostgreSQL":    {"label": "PostgreSQL",    "fields": ["host", "port (default 5432)", "database name", "username", "password"], "url_var": "DATABASE_URL or POSTGRES_*"},
        "MySQL":         {"label": "MySQL",          "fields": ["host", "port (default 3306)", "database name", "username", "password"], "url_var": "DATABASE_URL or MYSQL_*"},
        "MongoDB":       {"label": "MongoDB",        "fields": ["connection URI (mongodb://host:27017/dbname) or host/port/dbname/username/password"], "url_var": "MONGODB_URI or MONGO_*"},
        "Redis":         {"label": "Redis",          "fields": ["host", "port (default 6379)", "password (if auth enabled)"], "url_var": "REDIS_URL or REDIS_HOST/REDIS_PASSWORD"},
        "Elasticsearch": {"label": "Elasticsearch",  "fields": ["host", "port (default 9200)", "username", "password (if secured)"], "url_var": "ELASTIC_URL or ELASTICSEARCH_*"},
        "RabbitMQ":      {"label": "RabbitMQ",       "fields": ["host", "port (default 5672)", "vhost", "username", "password"], "url_var": "AMQP_URL or RABBITMQ_*"},
        "Kafka":         {"label": "Kafka",           "fields": ["broker host:port", "topic(s)", "consumer group"], "url_var": "KAFKA_BROKERS or KAFKA_*"},
    }

    # Match detected DB names from env vars too (in case code scan missed something)
    for ev in required_env:
        for db, meta in _DB_FIELDS.items():
            if db.upper().replace("SQL","").replace("SEARCH","") in ev and db not in detected_dbs:
                detected_dbs.append(db)

    # Separate out DB-related secret vars so we don't double-list them
    _DB_SECRET_PAT = re.compile(
        r'(?:DATABASE|POSTGRES|MYSQL|MONGO|REDIS|ELASTIC|RABBIT|AMQP|KAFKA)', re.I
    )
    db_secret_vars = [k for k in secret_vars if _DB_SECRET_PAT.search(k)]
    other_secret_vars = [k for k in secret_vars if k not in db_secret_vars]

    # ── Questions that need human answers ─────────────────────────────────────
    sections.append("\n**Questions that need your input:**")
    q = 1

    if not infra_hint:
        sections.append(f"  {q}. **Target infra** — Kubernetes cluster, VM/VPS, or Docker host?")
    else:
        sections.append(f"  {q}. **Target infra** — confirmed as {infra_hint}. Which environment(s)? (dev / staging / prod)")
    q += 1

    sections.append(f"  {q}. **Environments** — which do you need? (dev / staging / prod)")
    q += 1

    sections.append(f"  {q}. **Domain** — what URL should the app be reachable at?")
    q += 1

    # Per-database connection questions
    for db in detected_dbs:
        if db == "SQLite":
            continue  # SQLite needs no connection config
        meta = _DB_FIELDS.get(db)
        if not meta:
            continue
        # Check if .env.example already has vars for this DB
        known = [k for k in required_env if db.upper().replace("SQL","").replace("SEARCH","")[:5] in k or "DATABASE" in k]
        if known:
            sections.append(f"  {q}. **{meta['label']} connection** — provide values for: {', '.join(f'`{k}`' for k in known[:6])}")
        else:
            fields_str = ", ".join(meta["fields"])
            sections.append(f"  {q}. **{meta['label']} connection** — provide: {fields_str} (will be set as `{meta['url_var']}`)")
        q += 1

    # Other secrets (API keys, tokens, etc. not DB-related)
    if other_secret_vars:
        sections.append(f"  {q}. **Other secrets** — provide values for: {', '.join(f'`{k}`' for k in other_secret_vars[:8])}{'...' if len(other_secret_vars) > 8 else ''}")
        q += 1
    elif not required_env and not detected_dbs:
        sections.append(f"  {q}. **Secrets** — any API keys, passwords, or tokens the app needs?")
        q += 1

    return "\n".join(sections)
