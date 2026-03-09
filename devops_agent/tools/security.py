"""Security scanning tools -- code review, pre-deploy, and post-deploy checks."""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import ssl
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ..config import get_settings
from .discovery import clone_repo as _clone_repo

# ─── Secret patterns ──────────────────────────────────────────────────────────

_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("AWS Access Key",        re.compile(r"AKIA[0-9A-Z]{16}")),
    ("AWS Secret Key",        re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*['\"]?[A-Za-z0-9/+]{40}")),
    ("GitHub Token",          re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("Generic API key",       re.compile(r"(?i)(api_key|api-key|apikey)\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{20,}")),
    ("Private key header",    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("Hardcoded password",    re.compile(r"(?i)(password|passwd|pwd)\s*[=:]\s*['\"](?!.*\{)[^'\"]{6,}")),
    ("Slack webhook",         re.compile(r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+")),
    ("Stripe key",            re.compile(r"sk_(live|test)_[A-Za-z0-9]{24,}")),
    ("Generic secret",        re.compile(r"(?i)(secret|token)\s*[=:]\s*['\"](?!.*\{)[^'\"]{16,}")),
    ("Base64 blob (>40 chars)",re.compile(r"['\"][A-Za-z0-9+/]{40,}={0,2}['\"]")),
]

_TEST_PATH_FRAGMENTS = (
    "/test/", "/tests/", "/spec/", "/specs/", "test_", "_test.",
    "_spec.", "fixture", "mock", "/docs/", "/doc/", "/examples/", "/example/",
)

# Template/example file suffixes and names — never real secrets
_TEMPLATE_FILE_NAMES = {
    ".env.example", ".env.sample", ".env.template", ".env.dist",
    ".env.defaults", ".env.local.example",
}

# Placeholder value patterns — clearly not real secrets
_PLACEHOLDER_RE = re.compile(
    r"(?i)(your[_\-]|change[_\-]?me|replace[_\-]?(this|me|with)|"
    r"<[^>]+>|\[your[_\-]|todo|placeholder|example|changeme|"
    r"xxx+|yyy+|zzz+|aaa+|1234|fake|dummy|test123|secret123|"
    r"insert[_\-]|fill[_\-]?in|add[_\-]?your|put[_\-]?your)"
)

# Comment prefixes across languages (line-level)
_COMMENT_PREFIXES = ("//", "///", "#", "*", "/*", "<!--", "--", ";;", "!")

# Per-language test-block annotation patterns (appear a few lines above the code)
_TEST_CONTEXT_MARKERS = re.compile(
    r"#\[(?:cfg\(test\)|test)\]"          # Rust
    r"|func\s+Test\w"                      # Go
    r"|\bdef\s+test_"                      # Python
    r"|describe\(|it\(|test\("             # JS/TS
    r"|@pytest\.mark|@Test\b"             # pytest / JUnit
    r"|\bassert(?:_eq!|_ne!|_eq|Equal)\b"  # any assertion on same line
)


def _is_test_file(rel_path: str) -> bool:
    lower = rel_path.lower()
    basename = lower.split("/")[-1]
    return (
        any(f in lower for f in _TEST_PATH_FRAGMENTS)
        or lower.endswith((".md", ".rst", ".txt", ".mdx"))
        or basename in _TEMPLATE_FILE_NAMES
    )


def _is_comment(line: str) -> bool:
    s = line.strip()
    return any(s.startswith(p) for p in _COMMENT_PREFIXES)


def _in_test_context(lines: list[str], lineno: int, window: int = 30) -> bool:
    """Check whether lineno (1-based) sits inside a test block.

    Scans up to `window` lines above the hit for test annotations/functions,
    and also checks the line itself for assertion calls.
    """
    start = max(0, lineno - 1 - window)
    end = lineno  # inclusive of the hit line itself
    for ln in lines[start:end]:
        if _TEST_CONTEXT_MARKERS.search(ln):
            return True
    return False


def _is_charset_definition(value: str) -> bool:
    """Return True if the string looks like an encoding alphabet, not a secret.

    Encoding charsets (base64, base32, hex, etc.) consist of long sequential
    runs of ASCII characters (A→B→C…, a→b→c…, 0→1→2…).  Real encoded secrets
    have essentially random character ordering.

    We measure the fraction of adjacent pairs that are consecutive in ASCII
    order.  A charset scores near 1.0; random encoded data scores near 0.
    """
    if len(value) < 8:
        return False
    sequential = sum(
        1 for a, b in zip(value, value[1:]) if ord(b) == ord(a) + 1
    )
    return sequential / (len(value) - 1) > 0.5


_DANGEROUS_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("eval() usage",          re.compile(r"\beval\s*\(")),
    ("exec() usage",          re.compile(r"\bexec\s*\(")),
    ("subprocess shell=True", re.compile(r"subprocess\.(?:run|Popen|call)\b[^)]*shell\s*=\s*True")),
    ("os.system call",        re.compile(r"\bos\.system\s*\(")),
    ("Hardcoded 0.0.0.0",     re.compile(r"host\s*=\s*['\"]0\.0\.0\.0['\"]")),
    ("SQL concatenation",     re.compile(r"(?i)(execute|query)\s*\([^)]*\+[^)]*\)")),
    ("Pickle load",           re.compile(r"\bpickle\.loads?\s*\(")),
    ("yaml.load (unsafe)",    re.compile(r"\byaml\.load\s*\([^,)]*\)")),
    ("DEBUG=True",            re.compile(r"DEBUG\s*=\s*True")),
    ("verify=False (TLS)",    re.compile(r"verify\s*=\s*False")),
]

# Extensions to scan (all common source languages + config)
_SCAN_EXTENSIONS = {
    # Systems / compiled
    ".rs", ".go", ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".kt", ".scala",
    # Scripting
    ".py", ".rb", ".php", ".sh", ".bash", ".zsh", ".fish", ".ps1",
    # Web / JS
    ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
    # JVM
    ".java", ".groovy", ".kts",
    # Config / infra
    ".env", ".yaml", ".yml", ".json", ".toml", ".conf", ".ini", ".cfg",
    ".dockerfile", ".tf", ".hcl", ".nix",
    # Other
    ".ex", ".exs", ".ml", ".hs", ".lua", ".r",
}
_SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", "target", "vendor"}


# ─── GitHub repo fetcher ──────────────────────────────────────────────────────

def _make_gh_headers(project_token: str | None = None) -> dict:
    """Build GitHub API headers, preferring project_token over global GITHUB_TOKEN."""
    settings = get_settings()
    token = project_token or settings.github_token
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _fetch_repo_tree(github_repo: str, branch: str, project_token: str | None = None) -> list[dict]:
    """Return flat list of {path, content} dicts for scannable files."""
    headers = _make_gh_headers(project_token)

    base = f"https://api.github.com/repos/{github_repo}"
    try:
        with httpx.Client(headers=headers, timeout=15) as client:
            tree_resp = client.get(f"{base}/git/trees/{branch}?recursive=1")
            tree_resp.raise_for_status()
            tree = tree_resp.json().get("tree", [])

        files = []
        for node in tree:
            if node["type"] != "blob":
                continue
            path = node["path"]
            parts = Path(path).parts
            if any(p in _SKIP_DIRS for p in parts):
                continue
            if Path(path).suffix.lower() not in _SCAN_EXTENSIONS and Path(path).name not in {
                "Dockerfile", ".env", ".env.example", "Makefile",
            }:
                continue
            if node.get("size", 0) > 200_000:   # skip large files
                continue
            files.append({"path": path, "sha": node["sha"], "url": node["url"]})
        return files
    except Exception as exc:
        return [{"error": str(exc)}]


def _fetch_file_content(url: str, headers: dict) -> str:
    try:
        with httpx.Client(headers=headers, timeout=10) as client:
            r = client.get(url)
            r.raise_for_status()
            data = r.json()
            if data.get("encoding") == "base64":
                return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            return data.get("content", "")
    except Exception:
        return ""


# ─── Public tool functions ────────────────────────────────────────────────────

def security_review_repo(
    github_repo: str,
    branch: str = "main",
    max_files: int = 200,
    github_token: str | None = None,
) -> str:
    """Scan a GitHub repository for secrets and dangerous code patterns (local clone)."""
    tmpdir, br_or_err = _clone_repo(github_repo, branch, github_token)
    if tmpdir is None:
        return f"Failed to clone {github_repo}: {br_or_err}"

    try:
        return _scan_local(tmpdir, github_repo, br_or_err, max_files)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _scan_local(root: Path, github_repo: str, branch: str, max_files: int) -> str:
    """Scan a locally cloned repo for secrets and dangerous patterns."""
    # Collect ALL files (for total count) and scannable subset
    all_fps: list[Path] = []
    scannable: list[Path] = []
    extra_names = {"Dockerfile", ".env", ".env.example", "Makefile", ".gitignore"}
    for dirpath, dirnames, filenames in _os_walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            fp = Path(dirpath) / fname
            all_fps.append(fp)
            if fp.suffix.lower() in _SCAN_EXTENSIONS or fname in extra_names:
                scannable.append(fp)

    total_files = len(all_fps)

    # Detect deployment infrastructure (check all files, not just scannable)
    all_rel = [str(f.relative_to(root)) for f in all_fps]
    infra: list[str] = []
    if any("compose" in p.lower() and p.endswith((".yml", ".yaml")) for p in all_rel):
        infra.append("Docker Compose present")
    if any(p == "Dockerfile" or p.endswith("/Dockerfile") for p in all_rel):
        infra.append("Dockerfile present")
    if any(p.startswith(("deploy/", "charts/", "k8s/", "kubernetes/")) or "Chart.yaml" in p for p in all_rel):
        infra.append("Kubernetes/Helm manifests present")

    findings: list[dict] = []
    scanned = 0

    for fp in scannable[:max_files]:
        try:
            content = fp.read_text(errors="replace")
        except Exception:
            continue
        scanned += 1
        rel = str(fp.relative_to(root))
        in_test_file = _is_test_file(rel)
        file_lines = content.splitlines()
        for lineno, line in enumerate(file_lines, 1):
            for label, pat in _SECRET_PATTERNS:
                if pat.search(line):
                    stripped = line.strip()
                    # Skip comment lines — doc examples live in comments
                    if _is_comment(line):
                        continue
                    # Skip env-var references (value comes from environment)
                    if any(x in stripped for x in ["${", "{{", "env(", "os.environ", "process.env", "getenv"]):
                        continue
                    # Skip test/fixture/doc files
                    if in_test_file:
                        continue
                    # Skip lines inside test blocks (e.g. Rust #[cfg(test)],
                    # Go TestXxx, Python def test_, JS describe/it)
                    if _in_test_context(file_lines, lineno):
                        continue
                    # Skip encoding alphabet / charset constants — they are
                    # sequential by definition and not secrets
                    m = pat.search(line)
                    if m and _is_charset_definition(m.group(0).strip("'\"")):
                        continue
                    # Skip obvious placeholder values
                    if m and _PLACEHOLDER_RE.search(m.group(0)):
                        continue
                    findings.append({
                        "severity": "HIGH",
                        "type": "secret",
                        "rule": label,
                        "file": rel,
                        "line": lineno,
                        "snippet": stripped[:120],
                    })
            for label, pat in _DANGEROUS_PATTERNS:
                if pat.search(line):
                    findings.append({
                        "severity": "MEDIUM",
                        "type": "dangerous_pattern",
                        "rule": label,
                        "file": rel,
                        "line": lineno,
                        "snippet": line.strip()[:120],
                    })

    high = [f for f in findings if f["severity"] == "HIGH"]
    medium = [f for f in findings if f["severity"] == "MEDIUM"]

    skipped = total_files - len(scannable)
    skipped_note = f" ({skipped} skipped: binaries, docs, lockfiles)" if skipped else ""

    lines = [
        f"Security Review: {github_repo}@{branch}",
        f"Total files: {total_files}  |  Scanned: {scanned}{skipped_note}  |  {len(high)} HIGH  |  {len(medium)} MEDIUM findings",
        "",
    ]
    if infra:
        lines.append("Deployment infrastructure detected:")
        for item in infra:
            lines.append(f"  * {item}")
        lines.append("")
    if not findings:
        lines.append("No issues found.")
        return "\n".join(lines)

    if high:
        lines.append("HIGH severity (secrets/credentials):")
        for f in high[:20]:
            lines.append(f"  [{f['file']}:{f['line']}] {f['rule']}")
            lines.append(f"    {f['snippet']}")
        if len(high) > 20:
            lines.append(f"  ... and {len(high)-20} more HIGH findings")

    if medium:
        lines.append("")
        lines.append("MEDIUM severity (dangerous patterns):")
        for f in medium[:15]:
            lines.append(f"  [{f['file']}:{f['line']}] {f['rule']}")
            lines.append(f"    {f['snippet']}")
        if len(medium) > 15:
            lines.append(f"  ... and {len(medium)-15} more MEDIUM findings")

    lines.append("")
    lines.append(
        "Review findings carefully. High-severity items may expose credentials. "
        "Rotate any confirmed secrets immediately."
    )
    return "\n".join(lines)


def _os_walk(root: Path):
    """Fallback os.walk for Python < 3.12 where Path.walk() doesn't exist."""
    import os
    for dirpath, dirnames, filenames in os.walk(root):
        yield Path(dirpath), dirnames, filenames


def pre_deploy_security_check(
    github_repo: str,
    image_or_ref: str,
    branch: str = "main",
    github_token: str | None = None,
) -> str:
    """Run pre-deployment security checks: secrets scan + optional image scan."""
    results: list[str] = [
        f"Pre-deploy security check: {github_repo}  →  {image_or_ref}",
        "",
    ]
    passed = True

    # 1. Quick secrets scan (first 30 files only for speed)
    review = security_review_repo(github_repo, branch, max_files=30, github_token=github_token)
    high_count = int(re.search(r"(\d+) HIGH", review).group(1)) if re.search(r"(\d+) HIGH", review) else 0
    if high_count > 0:
        passed = False
        results.append(f"❌ Secrets scan: {high_count} HIGH-severity secrets detected in code")
        results.append("   → Refuse to deploy until secrets are rotated and removed from repo")
    else:
        results.append("✅ Secrets scan: No hardcoded secrets detected")

    # 2. Check for .env files committed
    headers = _make_gh_headers(github_token)
    try:
        with httpx.Client(headers=headers, timeout=10) as client:
            r = client.get(
                f"https://api.github.com/repos/{github_repo}/contents/.env",
                params={"ref": branch},
            )
        if r.status_code == 200:
            passed = False
            results.append("❌ .env file found in repository root — must not be committed")
        else:
            results.append("✅ No .env file committed to repo root")
    except Exception:
        results.append("⚠️  Could not check for committed .env file")

    # 3. Check Dockerfile for best practices
    try:
        with httpx.Client(headers=headers, timeout=10) as client:
            r = client.get(
                f"https://api.github.com/repos/{github_repo}/contents/Dockerfile",
                params={"ref": branch},
            )
        if r.status_code == 200:
            content = base64.b64decode(r.json()["content"]).decode("utf-8", errors="replace")
            df_issues = _check_dockerfile(content)
            if df_issues:
                results.append(f"🟡 Dockerfile issues ({len(df_issues)}):")
                for issue in df_issues:
                    results.append(f"   • {issue}")
            else:
                results.append("✅ Dockerfile: No common issues found")
        else:
            results.append("ℹ️  No Dockerfile found at repo root")
    except Exception:
        results.append("⚠️  Could not fetch Dockerfile")

    # 4. Try running trivy if available
    trivy_result = _run_trivy(image_or_ref)
    if trivy_result:
        results.append("")
        results.append(trivy_result)

    results.append("")
    results.append("VERDICT: " + ("✅ PASS — safe to deploy" if passed else "❌ FAIL — fix issues before deploying"))
    return "\n".join(results)


def post_deploy_security_check(
    url: str,
    environment: str = "",
) -> str:
    """Run post-deployment HTTP security checks against the deployed service."""
    results: list[str] = [
        f"Post-deploy security check: {url}  [{environment}]",
        "",
    ]
    passed = True

    # 1. Basic reachability + redirect
    try:
        resp = httpx.get(url, timeout=10, follow_redirects=True)
        results.append(f"✅ Reachable: HTTP {resp.status_code}")
        if resp.url != httpx.URL(url):
            results.append(f"   Redirected to: {resp.url}")

        # 2. Security headers
        header_checks = {
            "Strict-Transport-Security": "HSTS missing — enable for HTTPS",
            "X-Content-Type-Options":    "X-Content-Type-Options missing (nosniff recommended)",
            "X-Frame-Options":           "X-Frame-Options missing (clickjacking risk)",
            "Content-Security-Policy":   "Content-Security-Policy missing",
            "Referrer-Policy":           "Referrer-Policy missing",
        }
        missing_headers = []
        for header, msg in header_checks.items():
            if header not in resp.headers:
                missing_headers.append(msg)

        if missing_headers:
            results.append(f"🟡 Missing security headers ({len(missing_headers)}):")
            for m in missing_headers:
                results.append(f"   • {m}")
        else:
            results.append("✅ All key security headers present")

        # 3. Check server header exposure
        server = resp.headers.get("Server", "")
        if server:
            results.append(f"🟡 Server header exposes: '{server}' — consider hiding version info")

        # 4. Check for sensitive paths
        sensitive_paths = [
            "/.env", "/admin", "/api/docs", "/swagger", "/swagger-ui",
            "/actuator", "/actuator/health", "/.git/config", "/phpinfo.php",
            "/server-status", "/metrics",
        ]
        exposed: list[str] = []
        with httpx.Client(timeout=5, follow_redirects=False) as client:
            for path in sensitive_paths:
                try:
                    r = client.get(url.rstrip("/") + path)
                    if r.status_code in (200, 403):  # 403 = exists but blocked
                        exposed.append(f"{path} → HTTP {r.status_code}")
                except Exception:
                    pass

        if exposed:
            results.append(f"🟡 Sensitive paths accessible ({len(exposed)}):")
            for e in exposed:
                results.append(f"   • {e}")
        else:
            results.append("✅ No sensitive paths exposed")

    except httpx.ConnectError:
        passed = False
        results.append(f"❌ Cannot reach {url} — service may be down")
    except Exception as exc:
        results.append(f"⚠️  HTTP check failed: {exc}")

    # 5. TLS check (HTTPS only)
    if url.startswith("https://"):
        tls_result = _check_tls(url)
        results.append("")
        results.append(tls_result)

    results.append("")
    results.append("VERDICT: " + ("✅ PASS" if passed else "❌ FAIL — service unreachable"))
    return "\n".join(results)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _check_dockerfile(content: str) -> list[str]:
    issues = []
    if re.search(r"^FROM\s+\S+:latest\b", content, re.MULTILINE):
        issues.append("Using ':latest' tag — pin to a specific version for reproducibility")
    if not re.search(r"^USER\s+(?!root)\S+", content, re.MULTILINE):
        issues.append("No non-root USER directive — container may run as root")
    if re.search(r"^RUN\s+.*apt-get install(?!.*--no-install-recommends)", content, re.MULTILINE):
        issues.append("apt-get install without --no-install-recommends increases image size/attack surface")
    if re.search(r"COPY\s+\.\s+\.", content):
        issues.append("'COPY . .' copies everything including secrets — use .dockerignore")
    if re.search(r"^ENV\s+\S*(PASSWORD|SECRET|KEY|TOKEN)\S*\s*=\s*\S+", content, re.MULTILINE | re.IGNORECASE):
        issues.append("Secrets set via ENV — use runtime injection (docker secrets / k8s secrets)")
    return issues


def _run_trivy(image: str) -> str:
    """Run trivy if it's available in PATH."""
    try:
        r = subprocess.run(
            ["trivy", "image", "--severity", "HIGH,CRITICAL", "--quiet", "--format", "json", image],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode not in (0, 1):
            return ""
        data = json.loads(r.stdout or "{}")
        results_list = data.get("Results", [])
        vuln_count = sum(len(res.get("Vulnerabilities") or []) for res in results_list)
        critical = sum(
            1 for res in results_list
            for v in (res.get("Vulnerabilities") or [])
            if v.get("Severity") == "CRITICAL"
        )
        high = sum(
            1 for res in results_list
            for v in (res.get("Vulnerabilities") or [])
            if v.get("Severity") == "HIGH"
        )
        if vuln_count == 0:
            return "✅ Trivy image scan: No HIGH/CRITICAL CVEs found"
        return (
            f"❌ Trivy image scan: {vuln_count} vulnerabilities "
            f"({critical} CRITICAL, {high} HIGH) in {image}"
        )
    except FileNotFoundError:
        return "ℹ️  trivy not installed — skipping image CVE scan"
    except Exception as exc:
        return f"⚠️  trivy scan failed: {exc}"


def _check_tls(url: str) -> str:
    from urllib.parse import urlparse
    host = urlparse(url).hostname
    port = urlparse(url).port or 443
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(socket.create_connection((host, port), timeout=5), server_hostname=host) as sock:
            cert = sock.getpeercert()
            expiry_str = cert.get("notAfter", "")
            if expiry_str:
                expiry = datetime.strptime(expiry_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                days_left = (expiry - datetime.now(timezone.utc)).days
                if days_left < 14:
                    return f"❌ TLS cert expires in {days_left} days ({expiry_str})"
                elif days_left < 30:
                    return f"🟡 TLS cert expires in {days_left} days — renew soon"
                else:
                    return f"✅ TLS cert valid, expires in {days_left} days"
            return "✅ TLS certificate valid"
    except ssl.SSLError as e:
        return f"❌ TLS error: {e}"
    except Exception as e:
        return f"⚠️  TLS check failed: {e}"
