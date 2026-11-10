"""The app builders — real files, real commands, real previews.

Two surfaces share one workspace:

* **DarkWeb App Builder** — multi-file web apps (HTML / CSS / JS / Node),
* **Rust Program Builder** — real cargo projects (``Cargo.toml`` + ``src/*.rs``).

Every call is a real filesystem or process operation inside
``data/workbench/<build_id>/``: read, create, write, edit, delete, run, install
dependencies, start/stop a preview server, and ask Ollama to change the app.
The operator keeps prompting; each answer is a JSON file plan applied to disk.

Honest boundaries:

* there is **no shell** — a command is split into argv and executed with
  ``shell=False`` against an allow-list of developer tools, so ``; | && >`` are
  rejected instead of interpreted,
* the model can only write inside the project (validated twice: strict name
  pattern, then resolved-path prefix),
* nothing is charged here: this is an admin operator console. **deploy** is what
  publishes a project (``POST /api/workbench/projects/<id>/deploy``).
"""
from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from core import ollama
from core.config import settings
from core.errors import NotFound, ValidationError
from oneness_tor.services.store import Collection, data_dir, slug

PROJECTS = Collection(lambda: data_dir("workbench") / "projects.json", "build")

KINDS: dict[str, dict] = {
    "web": {"label": "DarkWeb web app", "entry": "index.html",
            "preview": "iframe",
            "default_command": "{python} -m http.server {port} --bind 127.0.0.1"},
    "rust": {"label": "Rust program", "entry": "src/main.rs",
             "preview": "iframe", "default_command": "cargo run --release"},
}
SUFFIX_LANGUAGE = {
    ".html": "html", ".htm": "html", ".css": "css", ".js": "javascript",
    ".mjs": "javascript", ".json": "json", ".md": "markdown", ".toml": "toml",
    ".rs": "rust", ".py": "python", ".yml": "yaml", ".yaml": "yaml",
    ".svg": "xml", ".xml": "xml", ".txt": "text", ".sh": "shell",
}
SKIP_DIRS = {"target", "node_modules", ".git", "__pycache__", ".venv", "dist",
             ".cargo", ".idea", ".next"}
SKIP_FILES = {".run.log"}
MAX_READ_BYTES = 512 * 1024
MAX_WRITE_BYTES = 2 * 1024 * 1024
MAX_TREE_ENTRIES = 500
MAX_PLAN_FILES = 24
#: Output budgets tried for one plan: the first ask, then one bigger retry when
#: the model ran out of tokens mid-file (``done_reason="length"``).
PLAN_BUDGETS = (4096, 8192)
#: Developer tools the workbench will execute (no shell, no arbitrary binaries).
ALLOWED_PROGRAMS = {
    "cargo", "rustc", "rustup", "python", "python3", "py", "pip", "pip3",
    "node", "npm", "npx", "git", "make", "dotnet", "go", "java", "javac",
    "pytest",
}
#: Anything a shell would interpret. The workbench takes one argv per call.
SHELL_OPERATORS = (";", "&&", "||", "|", ">", "<", "`", "$(", "%", "\n", "\r")

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,80}$")
_SERVERS: dict[str, dict] = {}
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp() -> int:
    return int(time.time())


def _fill(text: str, **values: str) -> str:
    for key, value in values.items():
        text = text.replace(f"__{key}__", value)
    return text


def _which(program: str) -> str | None:
    """shutil.which plus the usual per-user tool folders (cargo, pipx…)."""
    found = shutil.which(program)
    if found:
        return found
    home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    names = [program]
    if os.name == "nt" and not program.lower().endswith(".exe"):
        names.append(program + ".exe")
    for folder in (home / ".cargo" / "bin", home / ".local" / "bin",
                   Path("/usr/local/bin"), Path("/opt/homebrew/bin")):
        for name in names:
            candidate = folder / name
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
    return None


def _split(command: str) -> list[str]:
    """Split a command line into argv without ever invoking a shell."""
    text = str(command or "").strip()
    if not text:
        raise ValidationError("a command is required, e.g. `cargo build`")
    try:
        parts = shlex.split(text, posix=False)
    except ValueError as problem:
        raise ValidationError(f"cannot parse that command: {problem}") from problem
    cleaned = [part[1:-1] if len(part) > 1 and part[0] == part[-1]
               and part[0] in "\"'" else part for part in parts]
    return [part for part in cleaned if part]


def _guard(command: str) -> None:
    for operator in SHELL_OPERATORS:
        if operator in command:
            raise ValidationError(
                f"the workbench executes argv directly, not a shell — "
                f"{operator!r} is not allowed",
                details={"fix": "one command per call: `cargo build`, then "
                                "`cargo run --release`, then open the preview"})


def _clean_rel(value: object) -> str:
    """A project-relative path with no way out of the project directory."""
    text = str(value or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if not text:
        raise ValidationError("a file path is required, e.g. src/main.rs")
    if text.startswith("/") or text.startswith("~") or re.match(r"^[A-Za-z]:", text):
        raise ValidationError("use a path inside the project, not an absolute path",
                              details={"path": text[:120]})
    if len(text) > 180:
        raise ValidationError("that path is longer than 180 characters")
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValidationError("'.' and '..' are not allowed in a project path",
                              details={"path": text[:120]})
    for part in parts:
        if not _SEGMENT.match(part):
            raise ValidationError(
                "file names may only use letters, digits, '.', '_', '-' and spaces",
                details={"segment": part[:80]})
    return "/".join(parts)


def _language(rel: str) -> str:
    return SUFFIX_LANGUAGE.get(Path(str(rel)).suffix.lower(), "text")


def project_dir(build_id: str) -> Path:
    """``data/workbench/<build_id>/`` — created on first use."""
    row = PROJECTS.get(build_id)
    path = data_dir("workbench") / str(row["id"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path(build_id: str, rel: object, *, parents: bool = False) -> Path:
    root = project_dir(build_id).resolve()
    target = (root / _clean_rel(rel)).resolve()
    if target != root and root not in target.parents:
        raise ValidationError("that path escapes the project directory",
                              details={"path": str(rel)[:120]})
    if parents:
        target.parent.mkdir(parents=True, exist_ok=True)
    return target


def tree(build_id: str) -> list[dict]:
    """Every text file in the project (skipping build output and dot folders)."""
    root = project_dir(build_id)
    rows: list[dict] = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs
                         if name not in SKIP_DIRS and not name.startswith("."))
        for name in sorted(files):
            if name in SKIP_FILES or name.startswith("."):
                continue
            path = Path(folder) / name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            rel = path.relative_to(root).as_posix()
            rows.append({"path": rel, "bytes": size, "language": _language(rel)})
            if len(rows) >= MAX_TREE_ENTRIES:
                return rows
    rows.sort(key=lambda row: (row["path"].count("/"), row["path"]))
    return rows



# ------------------------------------------------------------------ scaffolds
WEB_INDEX = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="styles.css">
</head>
<body>
<header class="bar">
  <span class="dot" aria-hidden="true"></span>
  <strong>__TITLE__</strong>
  <span class="tag">built in the OneNess TOR workbench</span>
</header>
<main>
  <h1>Ship the <em>dark web</em> app.</h1>
  <p id="lede">Edit <code>index.html</code>, <code>styles.css</code> and
    <code>app.js</code>, or prompt the builder to change this page.</p>
  <section class="cards">
    <article><h2>Live node</h2><p id="health">reading /api/health…</p></article>
    <article><h2>Real files</h2><p>Everything here is a file in your project
      directory — create, edit, delete, run.</p></article>
    <article><h2>Preview</h2><p>This page is served by the node at
      <code>/builds/&lt;id&gt;/</code>.</p></article>
  </section>
</main>
<footer>__TITLE__ · OneNess TOR</footer>
<script src="app.js"></script>
</body>
</html>
"""

WEB_STYLES = """:root{--ink:#dff6ff;--dim:#8ea6c4;--cyan:#22f5ff;--magenta:#ff3bd4;
  --bg:#05060b;--panel:rgba(11,18,32,.82);--line:rgba(120,220,255,.18)}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;color:var(--ink);background:
  radial-gradient(1200px 600px at 70% -10%,rgba(255,59,212,.18),transparent 60%),
  radial-gradient(900px 500px at 10% 0%,rgba(34,245,255,.16),transparent 55%),var(--bg);
  font-family:ui-sans-serif,system-ui,"Segoe UI",sans-serif}
.bar{display:flex;align-items:center;gap:.6rem;padding:1rem 1.6rem;
  border-bottom:1px solid var(--line);backdrop-filter:blur(10px)}
.dot{width:10px;height:10px;border-radius:50%;background:var(--cyan);
  box-shadow:0 0 16px var(--cyan)}
.tag{margin-left:auto;font-size:.7rem;letter-spacing:.18em;text-transform:uppercase;
  color:var(--dim)}
main{max-width:980px;margin:0 auto;padding:3.2rem 1.6rem}
h1{font-size:clamp(2rem,4vw,3.2rem);line-height:1.05;margin:0 0 1rem}
h1 em{font-style:normal;color:transparent;background:linear-gradient(90deg,var(--cyan),
  var(--magenta));-webkit-background-clip:text;background-clip:text}
p{color:var(--dim);line-height:1.65}
code{color:#b9f4ff;background:rgba(34,245,255,.08);padding:.1rem .35rem;border-radius:6px}
.cards{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
  margin-top:2.4rem}
article{padding:1.1rem 1.2rem;border-radius:16px;border:1px solid var(--line);
  background:var(--panel)}
article h2{font-size:.8rem;letter-spacing:.16em;text-transform:uppercase;
  color:var(--cyan);margin:0 0 .5rem}
footer{padding:2rem 1.6rem;color:var(--dim);font-size:.75rem;text-align:center}
"""

WEB_SCRIPT = """/* __TITLE__ — the node is one fetch away. */
async function health() {
  const box = document.getElementById("health");
  try {
    const reply = await fetch("/api/health", { headers: { accept: "application/json" } });
    const payload = await reply.json();
    const data = payload.data || {};
    box.textContent = `node ${data.status} · height ${data.height ?? "—"} · chain ${data.chain_id ?? "—"}`;
  } catch (problem) {
    box.textContent = `node unreachable: ${problem.message}`;
  }
}
health();
"""

WEB_README = """# __TITLE__

A dark-web app scaffolded by the OneNess TOR workbench.

* `index.html` — the page (inline-friendly, no external requests)
* `styles.css` — neon theme
* `app.js` — reads `/api/health` from the node

Edit the files in `/workbench` (or prompt the builder), press **run** to
execute a command here, and **start** to serve this folder on a local port.
Publish for real with **deploy** (a real onion through Tor's control port).
"""


RUST_CARGO = """[package]
name = "__SLUG__"
version = "0.1.0"
edition = "2021"

[[bin]]
name = "__SLUG__"
path = "src/main.rs"

[profile.release]
opt-level = 3
strip = true
"""

RUST_MAIN = r"""//! __TITLE__ — a std-only HTTP server scaffolded by the OneNess TOR workbench.
//!
//! `cargo run --release` starts it; the workbench passes PORT for you, so the
//! preview window and the app agree on one port.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::time::{SystemTime, UNIX_EPOCH};

const PAGE: &str = r#"<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#05060b;
color:#8ea6c4;font-family:ui-monospace,Menlo,Consolas,monospace}
main{max-width:640px;padding:2rem;border:1px solid rgba(34,245,255,.25);
border-radius:18px;background:rgba(11,18,32,.9)}
h1{color:#22f5ff;font-size:1.3rem;margin:0 0 .6rem}
code,b{color:#ff3bd4}
p{line-height:1.6}
</style></head>
<body><main>
<h1>__TITLE__</h1>
<p>Rust program is live from <code>src/main.rs</code>. Edit the file in the
workbench, press <b>restart</b>, then reload this preview.</p>
<p id="health">reading /health…</p>
</main>
<script>
fetch('/health').then(r => r.json())
  .then(d => { document.getElementById('health').textContent = 'ok · unix ' + d.unix; })
  .catch(e => { document.getElementById('health').textContent = 'health failed: ' + e; });
</script>
</body></html>"#;

fn main() -> std::io::Result<()> {
    let port = std::env::var("PORT").unwrap_or_else(|_| "8099".to_string());
    let address = format!("127.0.0.1:{port}");
    let listener = TcpListener::bind(&address)?;
    println!("__TITLE__ listening on http://{address}");
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                if let Err(error) = serve(stream) {
                    eprintln!("connection failed: {error}");
                }
            }
            Err(error) => eprintln!("accept failed: {error}"),
        }
    }
    Ok(())
}

fn serve(mut stream: TcpStream) -> std::io::Result<()> {
    let mut buffer = [0u8; 2048];
    let read = stream.read(&mut buffer)?;
    let request = String::from_utf8_lossy(&buffer[..read]);
    let path = request.split_whitespace().nth(1).unwrap_or("/");
    let (content_type, body) = if path == "/health" {
        (
            "application/json",
            format!(r#"{{"ok":true,"service":"__SLUG__","unix":{}}}"#, now()),
        )
    } else {
        ("text/html; charset=utf-8", PAGE.to_string())
    };
    let response = format!(
        "HTTP/1.1 200 OK\r\ncontent-type: {content_type}\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(response.as_bytes())?;
    stream.flush()
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0)
}
"""

RUST_README = """# __TITLE__

A Rust program scaffolded by the OneNess TOR workbench (`__SLUG__`).

* `src/main.rs` — a std-only HTTP server: `/` serves the page, `/health` JSON
* `Cargo.toml` — release profile, no dependencies

```bash
cargo build --release      # or press "run" in /workbench
cargo run --release        # starts on $PORT (the workbench picks one)
```

Add a dependency with the install box (`cargo add serde_json`) and keep
prompting the builder — every answer is applied to these real files.
"""


def _crate_name(title: str) -> str:
    text = re.sub(r"[^a-z0-9-]+", "-", slug(title, "app")).strip("-") or "app"
    if not text[0].isalpha():
        text = "app-" + text
    return text[:48]


def _scaffold(kind: str, title: str, crate: str) -> dict[str, str]:
    if kind == "rust":
        return {"Cargo.toml": _fill(RUST_CARGO, TITLE=title, SLUG=crate),
                "src/main.rs": _fill(RUST_MAIN, TITLE=title, SLUG=crate),
                "README.md": _fill(RUST_README, TITLE=title, SLUG=crate)}
    return {"index.html": _fill(WEB_INDEX, TITLE=title),
            "styles.css": _fill(WEB_STYLES, TITLE=title),
            "app.js": _fill(WEB_SCRIPT, TITLE=title),
            "README.md": _fill(WEB_README, TITLE=title)}



# ------------------------------------------------------------------ projects
def preview_url(build_id: str) -> str:
    """The node serves the project folder at ``/builds/<id>/`` (real files)."""
    base = str(settings.get("paths.public_base_url")).rstrip("/")
    return f"{base}/builds/{build_id}/"


def create(name: object, kind: object = "web", *, description: object = "",
           model: object = None, owner_wallet: object = None,
           owner_address: object = None) -> dict:
    """Scaffold a real project: files on disk + one metadata record."""
    chosen = str(kind or "web").strip().lower()
    if chosen not in KINDS:
        raise ValidationError(f"kind must be one of {sorted(KINDS)}",
                              details={"received": str(kind)[:40]})
    title = str(name or "").strip()
    if not title:
        raise ValidationError("a project name is required, e.g. neon shop")
    if len(title) > 96:
        raise ValidationError("keep the project name under 96 characters")
    crate = _crate_name(title) if chosen == "rust" else ""
    row = PROJECTS.add({
        "name": slug(title, "app")[:48], "title": title, "kind": chosen,
        "description": str(description or "")[:400],
        "entry": KINDS[chosen]["entry"], "crate": crate,
        "surface": "rust_builder" if chosen == "rust" else "web_builder",
        "model": str(model or ""), "created_at": _now(), "created_unix": _stamp(),
        "updated_at": _now(), "files": 0, "edits": [], "last_run": None,
        "serve": None,
        # the account wallet that created it: only it (or the node admin) can
        # write, run, install, serve or deploy this project
        "owner_wallet": str(owner_wallet or ""),
        "owner_address": str(owner_address or ""),
    })
    files = _scaffold(chosen, title, crate or row["name"])
    for relative, content in files.items():
        _path(row["id"], relative, parents=True).write_text(content,
                                                           encoding="utf-8")
    PROJECTS.update(row["id"], files=len(files))
    return get_project(row["id"])


def list_projects(limit: int = 50) -> dict:
    rows = PROJECTS.all()
    rows.sort(key=lambda row: int(row.get("created_unix") or 0), reverse=True)
    rows = rows[:max(1, int(limit))]
    for row in rows:
        row["preview_url"] = preview_url(str(row.get("id")))
        row["kind_label"] = KINDS.get(str(row.get("kind")), {}).get(
            "label", str(row.get("kind")))
        row["running"] = _is_running(str(row.get("id")))
    return {"projects": rows, "count": len(PROJECTS.all()),
            "kinds": [{"id": key, **value} for key, value in KINDS.items()]}


def get_project(build_id: str, *, with_tree: bool = True) -> dict:
    row = dict(PROJECTS.get(build_id))
    row["kind_label"] = KINDS.get(str(row.get("kind")), {}).get(
        "label", str(row.get("kind")))
    row["preview_url"] = preview_url(row["id"])
    row["surface"] = ("rust_builder" if row.get("kind") == "rust"
                      else "web_builder")
    row["model_in_use"] = ollama.model_for(str(row["surface"]))
    row["running"] = _is_running(str(row["id"]))
    if with_tree:
        rows = tree(row["id"])
        row["tree"] = rows
        row["bytes"] = sum(int(item.get("bytes") or 0) for item in rows)
        row["files"] = len(rows)
    return row


def delete_project(build_id: str) -> dict:
    row = PROJECTS.get(build_id)
    stop_server(build_id)
    folder = data_dir("workbench") / str(row["id"])
    files = len(tree(row["id"]))
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    PROJECTS.save([item for item in PROJECTS.all()
                   if str(item.get("id")) != str(row["id"])])
    return {"deleted": row["id"], "files": files, "title": row.get("title")}



# --------------------------------------------------------------------- files
def read_file(build_id: str, rel: object) -> dict:
    path = _path(build_id, rel)
    if not path.is_file():
        raise NotFound(f"{_clean_rel(rel)} does not exist in {build_id}",
                       details={"project": build_id})
    size = path.stat().st_size
    if size > MAX_READ_BYTES:
        raise ValidationError(
            f"{_clean_rel(rel)} is {size} bytes — the editor reads at most "
            f"{MAX_READ_BYTES}", details={"path": _clean_rel(rel)})
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as problem:
        raise ValidationError(
            f"{_clean_rel(rel)} is binary — the editor only handles text",
            details={"path": _clean_rel(rel)}) from problem
    return {"path": _clean_rel(rel), "content": text, "bytes": size,
            "language": _language(rel), "lines": text.count("\n") + 1}


def write_file(build_id: str, rel: object, content: object,
               mode: str = "overwrite") -> dict:
    """Create, overwrite or append one text file inside the project."""
    choice = str(mode or "overwrite").strip().lower()
    if choice not in ("create", "overwrite", "append"):
        raise ValidationError("mode must be create, overwrite or append")
    if not isinstance(content, str):
        raise ValidationError("content must be a string (text files only)")
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise ValidationError(
            f"that file is {len(data)} bytes — the editor writes at most "
            f"{MAX_WRITE_BYTES}")
    path = _path(build_id, rel, parents=True)
    existed = path.is_file()
    if choice == "create" and existed:
        raise ValidationError(f"{_clean_rel(rel)} already exists",
                              details={"path": _clean_rel(rel)})
    if path.is_dir():
        raise ValidationError(f"{_clean_rel(rel)} is a directory")
    if choice == "append" and existed:
        with path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(content)
    else:
        path.write_text(content, encoding="utf-8")
    rows = tree(build_id)
    PROJECTS.update(build_id, files=len(rows), updated_at=_now())
    return {"path": _clean_rel(rel), "bytes": len(data),
            "created": not existed and choice != "append",
            "language": _language(rel), "lines": content.count("\n") + 1,
            "total_bytes": sum(int(item.get("bytes") or 0) for item in rows)}


def delete_file(build_id: str, rel: object) -> dict:
    path = _path(build_id, rel)
    if not path.is_file():
        raise NotFound(f"{_clean_rel(rel)} does not exist in {build_id}")
    size = path.stat().st_size
    path.unlink()
    parent, root = path.parent, project_dir(build_id).resolve()
    while parent != root and root in parent.parents:
        try:
            if any(parent.iterdir()):
                break
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
    rows = tree(build_id)
    PROJECTS.update(build_id, files=len(rows), updated_at=_now())
    return {"deleted": _clean_rel(rel), "bytes": size, "files": len(rows)}



# ----------------------------------------------------------------------- run
def _resolve_program(build_id: str, program: str) -> str:
    """Allow-listed developer tool, an absolute interpreter, or ``./local``."""
    root = project_dir(build_id).resolve()
    local = (root / program).resolve()
    is_local = (program.startswith(("./", ".\\")) and local.is_file()
                and root in local.parents)
    name = Path(program).name.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if not is_local and name not in ALLOWED_PROGRAMS:
        raise ValidationError(
            f"{Path(program).name!r} is not on the workbench allow-list",
            details={"allowed": sorted(ALLOWED_PROGRAMS),
                     "fix": "the workbench runs developer tools, not arbitrary programs"})
    found = str(local) if is_local else _which(program)
    if not found:
        raise ValidationError(
            f"{program!r} is not installed on this machine (not on PATH)",
            details={"allowed": sorted(ALLOWED_PROGRAMS)})
    return found


def _run(build_id: str, command: str, *, timeout: float = 120.0,
         argv: list[str] | None = None,
         extra_env: dict | None = None) -> dict:
    """Execute one command in the project directory and return the truth."""
    parts = [str(part) for part in argv] if argv else _split(str(command or ""))
    if not parts:
        raise ValidationError("a command is required, e.g. `cargo build`")
    if not argv:
        _guard(command)
    executable = _resolve_program(build_id, parts[0])
    limit = max(1.0, min(1800.0, float(timeout or 120)))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"}
    if extra_env:
        env.update({key: str(value) for key, value in extra_env.items()})
    started = time.time()
    timed_out, code = False, None
    try:
        finished = subprocess.run([executable, *parts[1:]],
                                  cwd=str(project_dir(build_id)),
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=limit, shell=False, env=env)
        stdout, stderr, code = finished.stdout, finished.stderr, finished.returncode
    except subprocess.TimeoutExpired as expired:
        timed_out = True
        stdout = expired.stdout if isinstance(expired.stdout, str) else (
            expired.stdout or b"").decode("utf-8", "replace")
        stderr = expired.stderr if isinstance(expired.stderr, str) else (
            expired.stderr or b"").decode("utf-8", "replace")
    except OSError as problem:
        raise ValidationError(f"cannot start {parts[0]!r}: {problem}") from problem
    duration = int((time.time() - started) * 1000)
    result = {"command": str(command or " ".join(parts)).strip(), "argv": parts,
              "exit_code": code, "ok": code == 0, "timed_out": timed_out,
              "duration_ms": duration, "stdout": (stdout or "")[-20000:],
              "stderr": (stderr or "")[-8000:],
              "stdout_truncated": len(stdout or "") > 20000,
              "timeout_seconds": limit}
    PROJECTS.update(build_id, updated_at=_now(),
                    last_run={"command": result["command"], "ok": result["ok"],
                              "exit_code": code, "at": _now(),
                              "duration_ms": duration})
    return result


def run_command(build_id: str, command: object, *, timeout: float = 120.0) -> dict:
    """``run`` in the UI: one argv, no shell, real output."""
    PROJECTS.get(build_id)
    return _run(build_id, str(command or ""), timeout=timeout)


def _pip_argv(packages: list[str]) -> list[str]:
    return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
            "--no-input", *packages]


def _npm_argv(packages: list[str]) -> list[str]:
    return ["npm", "install", "--no-audit", "--no-fund", *packages]


def _cargo_argv(packages: list[str]) -> list[str]:
    return ["cargo", "add", *packages] if packages else ["cargo", "fetch"]


MANAGERS: dict[str, dict] = {
    "pip": {"label": "Python (pip)", "argv": _pip_argv,
            "example": "requests flask"},
    "npm": {"label": "Node (npm)", "argv": _npm_argv,
            "example": "express ws"},
    "cargo": {"label": "Rust (cargo add / fetch)", "argv": _cargo_argv,
              "example": "serde serde_json"},
}


def install(build_id: str, manager: object = "pip", packages: object = None) -> dict:
    """Install dependencies for real (pip / npm / cargo) in the project folder."""
    row = PROJECTS.get(build_id)
    key = str(manager or "pip").strip().lower()
    if key not in MANAGERS:
        raise ValidationError(f"manager must be one of {sorted(MANAGERS)}",
                              details={"received": str(manager)[:40]})
    if isinstance(packages, (list, tuple)):
        names = [str(item).strip() for item in packages if str(item).strip()]
    else:
        text = str(packages or "").strip()
        names = _split(text) if text else []
    if key != "cargo" and not names:
        raise ValidationError(
            f"list at least one package to install, e.g. {MANAGERS[key]['example']}")
    if names and any(not re.match(r"^[@A-Za-z0-9][@A-Za-z0-9._:/=<>~^+-]{0,120}$", name)
                     for name in names):
        raise ValidationError("package names may only use letters, digits and "
                              "@ . _ : / = < > ~ ^ + -")
    argv = MANAGERS[key]["argv"](names)
    result = _run(build_id, " ".join(argv), timeout=900, argv=argv)
    return {**result, "manager": key, "manager_label": MANAGERS[key]["label"],
            "packages": names, "project": row["id"]}



# -------------------------------------------------------------------- serve
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _log_path(build_id: str) -> Path:
    return project_dir(build_id) / ".run.log"


def _is_running(build_id: str) -> bool:
    with _LOCK:
        entry = _SERVERS.get(str(build_id))
    return bool(entry and entry["process"].poll() is None)


def _default_argv(kind: str, port: int) -> list[str]:
    if kind == "rust":
        return ["cargo", "run", "--release"]
    return [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"]


def _kill(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, text=True, timeout=30)
    else:
        process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()


def serve(build_id: str, *, command: object = None, port: object = None) -> dict:
    """Start the project (static server or ``cargo run``) in the background."""
    row = PROJECTS.get(build_id)
    kind = str(row.get("kind") or "web")
    with _LOCK:
        existing = _SERVERS.get(build_id)
        if existing and existing["process"].poll() is None:
            return {**server_status(build_id), "already_running": True}
        chosen_port = int(port or _free_port())
        if not 1024 <= chosen_port <= 65535:
            raise ValidationError("port must be between 1024 and 65535")
        text = str(command or "").strip()
        if text:
            _guard(text)
            parts = [part.replace("{port}", str(chosen_port))
                     for part in _split(text)]
        else:
            parts = _default_argv(kind, chosen_port)
        executable = _resolve_program(build_id, parts[0])
        log_path = _log_path(build_id)
        try:
            handle = log_path.open("w", encoding="utf-8", errors="replace")
        except OSError as problem:
            raise ValidationError(f"cannot open {log_path.name}: {problem}") from problem
        env = {**os.environ, "PORT": str(chosen_port),
               "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"}
        flags = 0
        if os.name == "nt":
            flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                     | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            process = subprocess.Popen([executable, *parts[1:]],
                                       cwd=str(project_dir(build_id)),
                                       stdout=handle, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, shell=False,
                                       env=env, creationflags=flags)
        except OSError as problem:
            handle.close()
            raise ValidationError(f"cannot start {parts[0]!r}: {problem}") from problem
        finally:
            try:
                handle.close()
            except OSError:
                pass
        _SERVERS[build_id] = {"process": process, "command": " ".join(parts),
                              "port": chosen_port, "kind": kind,
                              "started_unix": _stamp(), "log": str(log_path)}
        PROJECTS.update(build_id, updated_at=_now(),
                        serve={"port": chosen_port, "command": " ".join(parts),
                               "started_at": _now()})
    deadline = time.time() + 1.5
    while time.time() < deadline:
        if _SERVERS[build_id]["process"].poll() is not None:
            break
        time.sleep(0.15)
    return server_status(build_id)


def server_status(build_id: str) -> dict:
    """Running state, port, preview URL and the tail of the real process log."""
    PROJECTS.get(build_id)
    with _LOCK:
        entry = _SERVERS.get(build_id)
    log_path = _log_path(build_id)
    log = ""
    if log_path.exists():
        try:
            log = log_path.read_text(encoding="utf-8", errors="replace")[-6000:]
        except OSError:
            log = ""
    if not entry:
        return {"running": False, "command": None, "port": None,
                "preview_url": None, "log": log, "exit_code": None,
                "started_unix": None, "pid": None}
    process = entry["process"]
    code = process.poll()
    running = code is None
    port = int(entry["port"])
    return {"running": running, "pid": process.pid,
            "command": entry["command"], "port": port,
            "started_unix": entry["started_unix"], "exit_code": code,
            "log": log, "kind": entry.get("kind"),
            "preview_url": f"http://127.0.0.1:{port}/" if running else None}


def stop_server(build_id: str) -> dict:
    with _LOCK:
        entry = _SERVERS.pop(str(build_id), None)
    if not entry:
        return {"stopped": False, "running": False,
                "note": "no preview server was started by this process"}
    _kill(entry["process"])
    PROJECTS.update(build_id, serve=None, updated_at=_now())
    return {"stopped": True, "running": False, "port": entry["port"],
            "command": entry["command"], "pid": entry["process"].pid}



# ----------------------------------------------------------------- AI edits
WEB_SYSTEM = """You are the DarkWeb App Builder of a OneNess TOR node.

The operator owns a real project directory on disk. You change it by returning a
JSON file plan — the workbench writes exactly those files, and the node serves
them at /builds/<id>/ so the operator sees the result immediately.

RULES
1. Reply with ONE JSON object and nothing else:
   {"answer": "<one short paragraph for the operator>",
    "files": [{"path": "relative/path", "content": "<the COMPLETE file>"}],
    "delete": ["relative/path"]}
2. Paths are relative to the project root — "index.html", "src/app.js". Never an
   absolute path and never "/workbench". The workspace in the user message is
   delimited by <<<FILE path>>> … <<<END FILE>>>; do not copy those markers into
   your answer and do not echo the workspace back.
3. Every file you return is written whole — never a diff, never "..." or
   "(unchanged)".
4. Self-contained only: no CDN, no external fonts or scripts, images as inline
   SVG, CSS gradients or data: URIs (the app is served from a Tor hidden service).
5. Aesthetic: deep blacks, cyan/magenta glow, glass panels, 3D transforms,
   responsive, accessible, fast.
6. Touch only the files that must change; list removals under "delete".
7. Node apps: add "server.js" plus "package.json" and say that the operator must
   install dependencies (npm) before starting the preview.
"""

RUST_SYSTEM = """You are the Rust Program Builder of a OneNess TOR node.

The operator owns a real cargo project on disk. You change it by returning a JSON
file plan — the workbench writes exactly those files, then `cargo build` and
`cargo run` decide whether the program is real.

RULES
1. Reply with ONE JSON object and nothing else:
   {"answer": "...", "files": [{"path": "src/main.rs", "content": "<whole file>"}],
    "delete": []}
2. Send complete files, never diffs and never "// unchanged". Paths are relative
   to the project root ("src/main.rs", "Cargo.toml") and the workspace is delimited
   by <<<FILE path>>> … <<<END FILE>>> — do not echo those markers back.
3. Keep Cargo.toml valid (package name, edition 2021, no default features you do
   not use). Only add a dependency when the operator asked for it — it must be
   installed with `cargo add` first.
4. The workbench starts the program with the PORT environment variable: a web app
   must bind 127.0.0.1:$PORT and print the URL it listens on.
5. Prefer the standard library unless a crate is truly required. Use Result and
   print useful errors instead of panicking.
"""

FENCE_SUFFIX = {"html": "index.html", "htm": "index.html", "css": "styles.css",
                "javascript": "app.js", "js": "app.js", "json": "data.json",
                "rust": "src/main.rs", "toml": "Cargo.toml", "python": "app.py",
                "markdown": "README.md", "md": "README.md"}


def _matching_object(text: str, start: int) -> str:
    """The balanced ``{...}`` slice starting at ``start`` (string-aware)."""
    depth, in_string, escape = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return text[start:]


def _loose_json(text: str) -> str:
    cleaned = re.sub(r"([{,]\s*)([A-Za-z_][\w-]*)\s*:", r'\1"\2":', text)
    return re.sub(r",\s*([}\]])", r"\1", cleaned)


def _plan_json(text: str) -> dict | None:
    """The first JSON object in the reply that looks like a file plan."""
    body = str(text or "").strip()
    if not body:
        return None
    start = body.find("{")
    while start != -1:
        chunk = _matching_object(body, start)
        parsed = None
        try:
            parsed = json.loads(chunk)
        except json.JSONDecodeError:
            try:
                parsed = json.loads(_loose_json(chunk))
            except json.JSONDecodeError:
                parsed = None
        if isinstance(parsed, dict) and ("files" in parsed or "delete" in parsed):
            return parsed
        start = body.find("{", start + 1)
    return None


def _fenced_plan(text: str, kind: str) -> dict | None:
    """Fallback for models that answer with ```html / ```rust blocks."""
    files = []
    for language, body in re.findall(r"```([A-Za-z0-9]+)?\n(.*?)```", text, re.S):
        path = FENCE_SUFFIX.get((language or "").lower())
        if not path or not body.strip():
            continue
        if kind == "rust" and path in ("index.html", "styles.css", "app.js"):
            continue
        files.append({"path": path, "content": body.rstrip() + "\n"})
    if not files:
        return None
    answer = re.sub(r"```.*?```", "", text, flags=re.S).strip()
    return {"answer": answer[:1200], "files": files, "delete": []}


def _salvage_plan(text: str) -> dict | None:
    """Whole files recovered from a reply that was cut off mid-JSON.

    A truncated plan is not a failed plan: every ``{"path": …, "content": …}``
    object that closed before the token limit is real, so the workbench writes
    those and tells the operator the rest is missing instead of throwing the
    complete files away.
    """
    body = str(text or "")
    files: list[dict] = []
    for match in re.finditer(r'\{\s*"path"\s*:', body):
        try:
            item = json.loads(_matching_object(body, match.start()))
        except json.JSONDecodeError:
            continue
        if (isinstance(item, dict) and item.get("path")
                and isinstance(item.get("content"), str)):
            files.append({"path": item["path"], "content": item["content"]})
    if not files:
        return None
    return {"answer": "", "files": files, "delete": [], "salvaged": True}


def _plan_reply(chosen: str, prompt: str, *, system: str,
                kind: str) -> tuple[dict, str]:
    """Ask one model for a JSON file plan — ``(plan, note)``.

    Reasoning is switched off for thinking models (their trace can swallow the
    whole budget, leaving ``response`` empty), a reply cut off at the token
    limit is retried once with a bigger budget, and complete files are salvaged
    from a still-truncated reply. ``note`` is the honest warning to show the
    operator; it is ``""`` when the plan came back whole.
    """
    think = False if ollama.supports_thinking(chosen) else None
    raw, done, thinking, budget = "", "", 0, 0
    for budget in PLAN_BUDGETS:
        reply = ollama.generate(
            chosen, prompt, system=system,
            options={"temperature": 0.35, "num_predict": budget},
            format="json", think=think)
        raw = str(reply.get("response") or "")
        done = str(reply.get("done_reason") or "")
        thinking = len(str(reply.get("thinking") or ""))
        plan = _plan_json(raw) or _fenced_plan(raw, kind)
        if plan is not None:
            return plan, ""
        if done != "length":
            break
    salvaged = _salvage_plan(raw)
    if salvaged is not None:
        return salvaged, (
            f"the reply hit the {budget}-token limit — only the "
            f"{len(salvaged['files'])} complete file(s) were applied; ask again "
            f"for the ones still missing")
    details = {"model": chosen, "done_reason": done or None, "budget": budget,
               "thinking_chars": thinking, "preview": raw[:400]}
    if not raw.strip():
        raise ValidationError(
            ("the model spent every token thinking and never reached a JSON "
             "file plan — pick a non-reasoning model (e.g. a qwen or gemma "
             "build) or raise the token budget")
            if thinking else
            "the model returned an empty reply instead of a JSON file plan",
            details=details)
    if done == "length":
        raise ValidationError(
            f"the model's JSON file plan was cut off at {budget} tokens before "
            f"one file closed — ask for fewer files at a time", details=details)
    raise ValidationError(
        "the model did not return a JSON file plan — try another model or a "
        "clearer instruction", details=details)


def _workspace(build_id: str, limit: int = 18000) -> str:
    """The project as the model sees it: path + full text, capped in size."""
    chunks, budget = [], limit
    for row in tree(build_id):
        if int(row.get("bytes") or 0) > 8000:
            continue
        try:
            text = _path(build_id, row["path"]).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(text) + 40 > budget:
            continue
        budget -= len(text) + 40
        chunks.append(f"<<<FILE {row['path']}>>>\n{text}\n<<<END FILE>>>")
    return "\n\n".join(chunks) or "(empty project)"



def ai_edit(build_id: str, prompt: object, *, model: object = None,
            history: object = None, apply: bool = True) -> dict:
    """Ask the surface's model for a JSON file plan and apply it to disk."""
    row = PROJECTS.get(build_id)
    text = str(prompt or "").strip()
    if not text:
        raise ValidationError("a prompt is required — say what to build or change")
    kind = str(row.get("kind") or "web")
    surface = "rust_builder" if kind == "rust" else "web_builder"
    chosen = ollama.pick_model(model, purpose=surface)
    parts = [f"# project: {row.get('title')} ({kind})",
             f"# entry point: {row.get('entry')}",
             "# current files", _workspace(build_id), "# request", text]
    turns = []
    for turn in (history if isinstance(history, list) else [])[-8:]:
        if not isinstance(turn, dict):
            continue
        who = "operator" if str(turn.get("role")) == "user" else "assistant"
        said = str(turn.get("content") or "").strip()
        if said:
            turns.append(f"{who}: {said[:700]}")
    if turns:
        parts += ["# earlier turns", "\n".join(turns)]
    parts.append("Reply with the JSON file plan only.")
    plan, plan_note = _plan_reply(
        chosen, "\n\n".join(parts),
        system=RUST_SYSTEM if kind == "rust" else WEB_SYSTEM, kind=kind)
    files = plan.get("files") if isinstance(plan.get("files"), list) else []
    removals = plan.get("delete") if isinstance(plan.get("delete"), list) else []
    if len(files) > MAX_PLAN_FILES:
        raise ValidationError(
            f"the model returned {len(files)} files — at most {MAX_PLAN_FILES} "
            f"are applied per edit")
    applied: list[dict] = []
    removed: list[str] = []
    warnings: list[str] = [plan_note] if plan_note else []
    entry = str(row.get("entry") or KINDS.get(kind, {}).get("entry") or "")
    snapshot, entry_path = "", None
    if entry:
        try:
            entry_path = _path(build_id, entry)
            if entry_path.is_file():
                snapshot = entry_path.read_text(encoding="utf-8")
        except (NotFound, ValidationError, OSError):
            snapshot, entry_path = "", None
    if apply:
        for item in files:
            if not isinstance(item, dict):
                continue
            path = item.get("path") or item.get("file")
            content = item.get("content")
            if path is None or content is None:
                continue
            written = write_file(build_id, path, content)
            applied.append({"path": written["path"],
                            "action": "created" if written["created"] else "updated",
                            "bytes": written["bytes"],
                            "language": written["language"]})
        for target in removals:
            try:
                relative = _clean_rel(target)
            except ValidationError as problem:
                warnings.append(f"kept {str(target)[:60]!r}: {problem.message}")
                continue
            if entry and relative == entry:
                warnings.append(f"kept {entry}: it is the entry point the preview serves")
                continue
            try:
                removed.append(delete_file(build_id, relative)["deleted"])
            except (NotFound, ValidationError) as problem:
                warnings.append(f"skipped {relative}: "
                                f"{getattr(problem, 'message', problem)}")
        if entry and snapshot and not _path(build_id, entry).is_file():
            _path(build_id, entry, parents=True).write_text(snapshot, encoding="utf-8")
            warnings.append(f"restored {entry}: a project without its entry point "
                            f"cannot run or preview")
        elif entry and not _path(build_id, entry).is_file():
            warnings.append(f"{entry} is missing — write it before starting the preview")
    planned = [str(item.get("path") or item.get("file"))
               for item in files if isinstance(item, dict)
               and (item.get("path") or item.get("file"))]
    edit = {"prompt": text[:600], "model": chosen, "surface": surface,
            "answer": str(plan.get("answer") or "")[:1500],
            "files": [item["path"] for item in applied] if apply else planned,
            "removed": removed, "warnings": warnings, "applied": bool(apply),
            "at": _now()}
    edits = list(row.get("edits") or [])
    edits.append(edit)
    PROJECTS.update(build_id, edits=edits[-20:], model=chosen, updated_at=_now(),
                    files=len(tree(build_id)))
    return {"answer": edit["answer"], "model": chosen, "purpose": surface,
            "applied": applied, "removed": removed, "warnings": warnings,
            "edit": edit, "tree": tree(build_id), "project": build_id,
            "model_in_use": ollama.model_for(surface)}


# ------------------------------------------------------------------ preview
def preview_file(build_id: str, asset: object = "") -> tuple[bytes, str]:
    """Bytes + mimetype for ``/builds/<id>/<asset>`` (real files, no traversal)."""
    row = PROJECTS.get(build_id)
    rel = str(asset or "").strip().strip("/") or str(row.get("entry")
                                                     or "index.html")
    path = _path(build_id, rel)
    if path.is_dir():
        path = _path(build_id, f"{rel.rstrip('/')}/{row.get('entry') or 'index.html'}")
    if not path.is_file():
        raise NotFound(f"{rel} does not exist in {build_id}",
                       details={"project": build_id})
    if path.stat().st_size > MAX_WRITE_BYTES:
        raise ValidationError(f"{rel} is too large to preview")
    mime = mimetypes.guess_type(path.name)[0] or "text/plain"
    if path.suffix.lower() in (".js", ".mjs"):
        mime = "text/javascript"
    elif path.suffix.lower() in (".md", ".rs", ".toml"):
        mime = "text/plain"
    if mime.startswith("text/") or mime in ("application/json", "image/svg+xml"):
        mime = f"{mime}; charset=utf-8"
    return path.read_bytes(), mime


# ------------------------------------------------------------------- deploy
def binary_path(build_id: str) -> Path | None:
    """The compiled Rust binary this project produced (``target/release``)."""
    row = PROJECTS.get(build_id)
    crate = str(row.get("crate") or row.get("name") or "app")
    base = project_dir(build_id) / "target" / "release"
    if not base.is_dir():
        return None
    names = [crate, crate.replace("-", "_")]
    suffixes = (".exe", "") if os.name == "nt" else ("",)
    for name in names:
        for suffix in suffixes:
            candidate = base / f"{name}{suffix}"
            if candidate.is_file():
                return candidate
    for candidate in sorted(base.glob("*.exe" if os.name == "nt" else "*")):
        if candidate.is_file():
            return candidate
    return None


def run_program(build_id: str, *, timeout: float = 60.0) -> dict:
    """Run the compiled Rust program (owner management action)."""
    row = PROJECTS.get(build_id)
    if str(row.get("kind")) != "rust":
        raise ValidationError(
            "only a Rust program runs natively — a DarkWeb app is served over Tor",
            details={"kind": row.get("kind")})
    binary = binary_path(build_id)
    if binary is None:
        raise ValidationError(
            "no compiled binary yet — deploy (cargo build --release) first",
            details={"project": build_id})
    relative = "./" + binary.relative_to(project_dir(build_id)).as_posix()
    return _run(build_id, relative, timeout=max(1.0, min(300.0, float(timeout))))


def deploy(build_id: str, *, wallet: str = "founder",
           owner_wallet: str | None = None) -> dict:
    """Deploy a builder project for real.

    * **web (DarkWeb App Builder)** → ``ADD_ONION`` for ``/builds/<id>/`` and a
      real ``http://<id>.onion/builds/<id>/`` URL (honest error when Tor's
      control port is not reachable).
    * **rust (Rust Program Builder)** → ``cargo build --release``, sha3 of the
      binary, then a program record whose owner is ``owner_wallet``; only that
      wallet (or an admin credential) can read its details or manage it.
    """
    from oneness_tor.services import contracts, ledger, tor_service

    row = PROJECTS.get(build_id)
    kind = str(row.get("kind") or "web")
    owner = str(owner_wallet or wallet or "founder")
    deployed_at = _now()
    if kind == "web":
        if not (_path(build_id, "index.html").is_file()):
            raise ValidationError("a DarkWeb app needs index.html before deploying")
        port = int(settings.get("api.port") or 8090)
        published = tor_service.publish_project(build_id, port)
        deployment = {"kind": "web", "onion": published["onion"],
                      "onion_url": published["url"], "service_id": published["service_id"],
                      "path": published["path"], "port": port,
                      "deployed_at": deployed_at}
        program = None
    elif kind == "rust":
        built = _run(build_id, "cargo build --release", timeout=900)
        if not built.get("ok"):
            raise ValidationError(
                "cargo build --release failed — fix the code, then deploy again",
                details={"exit_code": built.get("exit_code"),
                         "stderr": (built.get("stderr") or "")[-1500:]})
        binary = binary_path(build_id)
        if binary is None:
            raise ValidationError("the build finished but produced no binary")
        source = _path(build_id, "src/main.rs").read_text(encoding="utf-8")
        digest = hashlib.sha3_256(binary.read_bytes()).hexdigest()
        program = contracts.deploy_program(
            build_id=build_id, name=str(row.get("title") or build_id),
            wallet=owner, source=source, binary_path=str(binary),
            binary_sha3=digest, crate=str(row.get("crate") or ""),
            compile={"checked": True, "ok": True, "ms": built.get("duration_ms"),
                     "exit_code": built.get("exit_code")})
        deployment = {"kind": "rust", "program_id": program["id"],
                      "binary": str(binary), "binary_sha3": digest,
                      "crate": str(row.get("crate") or ""),
                      "build_ms": built.get("duration_ms"),
                      "deployed_at": deployed_at}
    else:
        raise ValidationError(f"unknown project kind {kind!r}")
    owner_view = ledger.load_wallet(owner) if program else None
    updated = PROJECTS.update(
        build_id, deployment=deployment, owner_wallet=owner,
        owner_address=(owner_view or {}).get("address") if owner_view else None,
        deployed_at=deployed_at)
    return {"project": get_project(build_id), "deployment": deployment,
            "program": program, "owner_wallet": owner,
            "owner_address": (owner_view or {}).get("address") if owner_view else None,
            "project_id": updated["id"],
            "note": ("served from this node over Tor; the onion points at "
                     f"{deployment.get('path') or 'the deployed program'}"
                     if kind == "web" else
                     "the program record is owner-only: view/manage it with the "
                     "owner wallet session")}

