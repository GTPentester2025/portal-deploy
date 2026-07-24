#!/usr/bin/env python3
"""
deploy.py — Full deployment of the Security & Risk Portal on RHEL 9.8.

A single nginx site that serves a home/launcher page plus seven internal tools:

    /                              Home launcher (2 categories: Awareness, TPRM)
    /newsletter/                   Awareness → "Newsletter"               (awareness-latest)
    /training-status/              Awareness → "Training Status Tracking" (training-UAT)
    /gophish-support/              Awareness → "Gophish Support"          (gophish_support)
    /userbase-automation/          Awareness → "Userbase Automation"      (Userbase Automation)
    /poster-app/                   Awareness → "Poster app"               (poster-app)
    /prp-charts/                   TPRM      → "PRP Charts"               (PRP-UAT)
    /panorays-intel471-censys/     TPRM      → "Panorays/Intel 471/Censys wireframe"
                                              (Panorays_Intel471_Censys_Dashboard_Wireframe)

Two of the tools are backends run as systemd services and reverse-proxied:
Gophish Support (Flask/gunicorn on 5050) and Poster app (Node/Express on 4180,
token-gated — deploy.py prints the tokenized first-visit URL).

Four of the tools are static (or SPA) files served directly by nginx. Gophish
Support is a Flask backend: deploy.py installs it into /opt/gophish-support,
runs it under gunicorn as a systemd service on 127.0.0.1:5000, and nginx
reverse-proxies /gophish-support/ to it.

Layout — deploy.py + portal/ ship together (the "portal-deploy" repo); the other
app folders are cloned as siblings. deploy.py finds each app whether it sits next
to deploy.py or one level up, so nothing needs copying after cloning. Typical VM:

    ~/apps/
      portal-deploy/   ← clone of portal-deploy (contains deploy.py + portal/)
      awareness-latest/  training-UAT/  gophish_support/
      Userbase-Automation/  Panorays_Intel471_Censys_Dashboard_Wireframe/  PRP-UAT/

Clone the repos as siblings, then run in place (no copying):

    cd ~/apps/portal-deploy
    sudo python3 deploy.py

What it does (10 steps):
  1  Preflight checks (root, RHEL, project layout)
  2  Install Node.js 20 via dnf module stream (needed to build the Newsletter app)
  3  Install nginx via dnf
  4  Build the Newsletter production artifact (dist/) with npm ci + build-dist
  5  Deploy home page + static apps to /var/www/portal/
  6  Deploy Gophish Support backend (venv + gunicorn + systemd service)
  7  Write nginx config  (/etc/nginx/conf.d/portal.conf)
  8  Fix SELinux file context + allow nginx→backend proxy
  9  Open port 80 in firewalld
 10  Enable + start nginx, health-check

Re-running is safe — all steps are idempotent.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
# deploy.py + portal/ ship together in the "portal-deploy" repo. The other app
# folders are cloned as siblings. So an app dir may live either next to deploy.py
# (flat layout) OR one level up (deploy.py inside portal-deploy/, apps in parent).
# _find_dir() locates each app across those candidate roots so nothing has to be
# copied around after cloning.
SCRIPT_DIR = Path(__file__).parent.resolve()
_APP_ROOTS = [SCRIPT_DIR, SCRIPT_DIR.parent]

# Used only for messages / "project root" display.
PROJECT_ROOT = SCRIPT_DIR


def _find_dir(*names: str, roots: list[Path] = _APP_ROOTS) -> Path:
    """First existing <root>/<name> across candidate roots (roots outermost loop,
    so SCRIPT_DIR wins over its parent). Falls back to roots[0]/names[0] for a
    sensible path in 'not found' error messages."""
    for root in roots:
        for name in names:
            candidate = root / name
            if candidate.is_dir():
                return candidate
    return roots[0] / names[0]


# Home launcher — always the copy bundled beside deploy.py (portal-deploy/portal),
# never a stale sibling; SCRIPT_DIR is tried first.
PORTAL_DIR = _find_dir("portal")

# Newsletter (Awareness) — an SPA with an npm build → dist/.
NEWSLETTER_SRC  = _find_dir("awareness-latest")
NEWSLETTER_DIST = NEWSLETTER_SRC / "dist"

# PRP Charts (TPRM) — self-contained static app (vendor libs load in the browser).
PRP_SRC = _find_dir("PRP-UAT")

# Training Status Tracking (Awareness) — static browser-only dashboard.
TRAINING_SRC = _find_dir("training-UAT")

# Userbase Automation (Awareness) — browser-only static app; entry lives in web/.
# Accept either the git repo name ("Userbase-Automation") or the spaced folder name.
USERBASE_SRC = _find_dir("Userbase-Automation", "Userbase Automation")

# Panorays/Intel 471/Censys dashboard wireframe (TPRM) — static (index.html + support.js).
PANORAYS_SRC = _find_dir("Panorays_Intel471_Censys_Dashboard_Wireframe")

# Gophish Support (Awareness) — Flask backend run under gunicorn + systemd.
GOPHISH_SRC     = _find_dir("gophish_support")
GOPHISH_APP_DIR = Path("/opt/gophish-support")           # deployed code + venv
GOPHISH_VENV    = GOPHISH_APP_DIR / "venv"
GOPHISH_SERVICE = Path("/etc/systemd/system/gophish-support.service")
GOPHISH_BIND    = "127.0.0.1:5050"   # 5000 is taken by the host agent on the VM

# Poster app (Awareness) — Node/Express backend run as a systemd service.
POSTER_SRC     = _find_dir("poster-app")
POSTER_APP_DIR = Path("/opt/poster-app")             # deployed code + node_modules
POSTER_SERVICE = Path("/etc/systemd/system/poster-app.service")
POSTER_PORT    = 4180                                 # its default (POSTER_APP_PORT)
POSTER_BIND    = f"127.0.0.1:{POSTER_PORT}"

WEB_ROOT      = Path("/var/www/portal")
NGINX_CONF    = Path("/etc/nginx/conf.d/portal.conf")
NGINX_DEFAULT = Path("/etc/nginx/conf.d/default.conf")

# Subpaths under the web root.
NEWSLETTER_WEB = WEB_ROOT / "newsletter"
PRP_WEB        = WEB_ROOT / "prp-charts"
TRAINING_WEB   = WEB_ROOT / "training-status"
USERBASE_WEB   = WEB_ROOT / "userbase-automation"
PANORAYS_WEB   = WEB_ROOT / "panorays-intel471-censys"

# ── ANSI colours (disabled if not a tty) ─────────────────────────────────────
_tty = sys.stdout.isatty()
def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty else text

def banner(msg: str) -> None:
    line = "─" * 62
    print(f"\n{_c('1;36', line)}\n{_c('1;36', '  ' + msg)}\n{_c('1;36', line)}")

def step(n: int, total: int, msg: str) -> None:
    print(f"\n{_c('1', f'[{n}/{total}] {msg}')}")

def ok(msg: str)   -> None: print(f"  {_c('0;32', '✓')}  {msg}")
def warn(msg: str) -> None: print(f"  {_c('1;33', '⚠')}  {msg}")
def info(msg: str) -> None: print(f"  {_c('0;36', '→')}  {msg}")
def err(msg: str)  -> None: print(f"  {_c('0;31', '✗')}  {msg}", file=sys.stderr)

def die(msg: str, code: int = 1) -> None:
    err(msg)
    sys.exit(code)

# ── Shell helper ──────────────────────────────────────────────────────────────
def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    cwd: Path | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        check=False,
        cwd=cwd,
        env={**os.environ, **(env or {})},
    )
    if check and result.returncode != 0:
        if capture and result.stderr:
            print(result.stderr, file=sys.stderr)
        die(f"Command failed (exit {result.returncode}): {' '.join(str(c) for c in cmd)}")
    return result

# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Preflight
# ─────────────────────────────────────────────────────────────────────────────
def check_root() -> None:
    if os.geteuid() != 0:
        die("Must run as root or with sudo:\n\n    sudo python3 deploy.py\n")
    ok("Running as root")

def check_rhel() -> None:
    osr = Path("/etc/os-release")
    if not osr.exists():
        warn("Cannot verify OS — /etc/os-release missing. Continuing.")
        return
    content = osr.read_text().lower()
    if "rhel" not in content and "red hat" not in content:
        warn("Non-RHEL OS detected. Script is designed for RHEL 9 — continuing anyway.")
    else:
        for line in osr.read_text().splitlines():
            if line.startswith("VERSION_ID="):
                ver = line.split("=", 1)[1].strip('"')
                if not ver.startswith("9"):
                    warn(f"RHEL version {ver} detected — designed for 9.x. Continuing.")
                else:
                    ok(f"RHEL {ver} confirmed")
                break

def check_project() -> None:
    problems: list[str] = []

    # Home launcher.
    if not (PORTAL_DIR / "index.html").exists():
        problems.append(f"portal/index.html missing at {PORTAL_DIR}")

    # Newsletter.
    if not NEWSLETTER_SRC.is_dir():
        problems.append(f"Newsletter app not found: {NEWSLETTER_SRC}")
    elif not (NEWSLETTER_SRC / "package.json").exists():
        problems.append(f"Newsletter package.json missing: {NEWSLETTER_SRC / 'package.json'}")

    # PRP Charts.
    if not (PRP_SRC / "index.html").exists():
        problems.append(f"PRP Charts index.html missing: {PRP_SRC / 'index.html'}")

    # Training Status Tracking.
    if not (TRAINING_SRC / "dashboard" / "index.html").exists():
        problems.append(
            f"Training dashboard missing: {TRAINING_SRC / 'dashboard' / 'index.html'}"
        )

    # Gophish Support (Flask backend).
    if not (GOPHISH_SRC / "gophish_manager.py").exists():
        problems.append(f"Gophish Support app missing: {GOPHISH_SRC / 'gophish_manager.py'}")
    elif not (GOPHISH_SRC / "requirements.txt").exists():
        problems.append(f"Gophish requirements.txt missing: {GOPHISH_SRC / 'requirements.txt'}")

    # Userbase Automation (static, entry in web/).
    if not (USERBASE_SRC / "web" / "index.html").exists():
        problems.append(
            f"Userbase Automation entry missing: {USERBASE_SRC / 'web' / 'index.html'}"
        )

    # Panorays/Intel 471/Censys dashboard wireframe (static).
    if not (PANORAYS_SRC / "index.html").exists():
        problems.append(
            f"Panorays wireframe index.html missing: {PANORAYS_SRC / 'index.html'}"
        )

    # Poster app (Node backend).
    if not (POSTER_SRC / "backend" / "server.js").exists():
        problems.append(f"Poster app server missing: {POSTER_SRC / 'backend' / 'server.js'}")
    elif not (POSTER_SRC / "package.json").exists():
        problems.append(f"Poster app package.json missing: {POSTER_SRC / 'package.json'}")

    if problems:
        die(
            "Project layout incomplete — copy the full project to this VM.\n    "
            + "\n    ".join(problems)
        )
    ok(f"deploy.py dir: {SCRIPT_DIR}")
    info(f"portal    → {PORTAL_DIR}")
    info(f"newsletter→ {NEWSLETTER_SRC}")
    info(f"training  → {TRAINING_SRC}")
    info(f"gophish   → {GOPHISH_SRC}")
    info(f"userbase  → {USERBASE_SRC}")
    info(f"panorays  → {PANORAYS_SRC}")
    info(f"prp       → {PRP_SRC}")
    ok("All app folders located")

# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Node.js 20
# ─────────────────────────────────────────────────────────────────────────────
def _node_version() -> str | None:
    node = shutil.which("node")
    if not node:
        return None
    r = run(["node", "--version"], capture=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else None

def install_nodejs() -> bool:
    """Install Node.js 20 via RHEL AppStream module. Returns True if available."""
    ver = _node_version()
    if ver:
        major = ver.lstrip("v").split(".")[0]
        if int(major) >= 20:
            ok(f"Node.js already installed: {ver}")
            return True
        else:
            warn(f"Node.js {ver} installed but need ≥20. Upgrading via dnf module.")

    info("Enabling nodejs:20 module stream …")
    r = run(["dnf", "module", "enable", "nodejs:20", "-y"], capture=True, check=False)
    if r.returncode != 0:
        if "already" in (r.stderr or "").lower() or "enabled" in (r.stdout or "").lower():
            pass  # fine
        else:
            warn("dnf module enable nodejs:20 failed. Trying NodeSource repo …")
            r2 = run(
                ["bash", "-c",
                 "curl -fsSL https://rpm.nodesource.com/setup_20.x | bash -"],
                capture=True, check=False,
            )
            if r2.returncode != 0:
                warn(
                    "NodeSource setup also failed.\n"
                    "  Node.js 20 is unavailable — will serve the Newsletter source "
                    "directory without building dist/."
                )
                return False

    info("Installing nodejs …")
    r = run(["dnf", "install", "nodejs", "-y"], capture=True, check=False)
    if r.returncode != 0:
        warn("dnf install nodejs failed — will serve Newsletter source without building dist/.")
        return False

    ver = _node_version()
    if ver:
        ok(f"Node.js installed: {ver}")
        return True

    warn("Node.js install may have failed — will serve Newsletter source as fallback.")
    return False

# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — nginx
# ─────────────────────────────────────────────────────────────────────────────
def install_nginx() -> None:
    if shutil.which("nginx"):
        r = run(["nginx", "-v"], capture=True, check=False)
        ver = (r.stderr or r.stdout).strip()
        ok(f"nginx already installed: {ver}")
        return
    info("Installing nginx …")
    run(["dnf", "install", "nginx", "-y"])
    ok("nginx installed")

# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Build the Newsletter app
# ─────────────────────────────────────────────────────────────────────────────
def build_newsletter() -> bool:
    """Run npm ci + build-dist.mjs in the Newsletter app. Returns True on success."""
    info("Running npm ci (Newsletter) …")
    r = run(["npm", "ci"], cwd=NEWSLETTER_SRC, capture=True, check=False)
    if r.returncode != 0:
        warn("npm ci failed — trying npm install …")
        r2 = run(["npm", "install"], cwd=NEWSLETTER_SRC, capture=True, check=False)
        if r2.returncode != 0:
            warn("npm install also failed — will serve Newsletter source as fallback.")
            return False
    ok("npm dependencies installed")

    info("Building Newsletter production artifact (dist/) …")
    r = run(
        ["node", "scripts/build-dist.mjs", "--force"],
        cwd=NEWSLETTER_SRC,
        capture=True,
        check=False,
    )
    if r.returncode != 0:
        warn("build-dist failed — will serve Newsletter source as fallback.")
        if r.stdout:
            print(r.stdout[-2000:])
        return False

    ok("Newsletter dist/ built successfully")
    return True

# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — Deploy files
# ─────────────────────────────────────────────────────────────────────────────
def _copy_tree(source_dir: Path, dest_dir: Path, *, delete: bool = True) -> None:
    """Copy the CONTENTS of source_dir into dest_dir (rsync if available)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    if shutil.which("rsync"):
        args = ["rsync", "-a"]
        if delete:
            args.append("--delete")
        run(args + [f"{source_dir}/", f"{dest_dir}/"])
    else:
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.copytree(source_dir, dest_dir)

def _copy_items(source_dir: Path, dest_dir: Path, items: list[str]) -> None:
    """Copy an explicit list of files/dirs from source_dir into dest_dir."""
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name in items:
        src = source_dir / name
        dst = dest_dir / name
        if not src.exists():
            warn(f"Skipping missing item: {src}")
            continue
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

def deploy_files(newsletter_is_dist: bool) -> None:
    info(f"Deploying home page + apps to {WEB_ROOT} …")
    WEB_ROOT.mkdir(parents=True, exist_ok=True)

    # ── Home launcher → web root (top-level files only; don't wipe app subdirs). ──
    for f in PORTAL_DIR.iterdir():
        if f.is_file():
            shutil.copy2(f, WEB_ROOT / f.name)
    ok("Home launcher deployed (index.html, health.html)")

    # ── Newsletter → /newsletter/ ──
    if newsletter_is_dist:
        _copy_tree(NEWSLETTER_DIST, NEWSLETTER_WEB)
        # dist/ excludes vendor/ (offline CDN fallbacks) — copy it explicitly.
        vendor_src = NEWSLETTER_SRC / "vendor"
        if vendor_src.is_dir():
            _copy_tree(vendor_src, NEWSLETTER_WEB / "vendor")
            ok("Newsletter vendor/ copied (offline CDN fallback)")
        ok("Newsletter deployed from dist/ → /newsletter/")
    else:
        _copy_tree(NEWSLETTER_SRC, NEWSLETTER_WEB)
        ok("Newsletter deployed from source → /newsletter/")

    # ── PRP Charts → /prp-charts/  (only the files the app needs). ──
    #    js/ is inlined into index.html; vendor/ holds the offline Plotly/xlsx libs.
    _copy_items(PRP_SRC, PRP_WEB, ["index.html", "logo.png", "vendor"])
    ok("PRP Charts deployed → /prp-charts/")

    # ── Training Status Tracking → /training-status/  (entry: dashboard/). ──
    training_items = ["dashboard", "shared"]
    if (TRAINING_SRC / "dash2.html").exists():
        training_items.append("dash2.html")
    _copy_items(TRAINING_SRC, TRAINING_WEB, training_items)
    ok("Training Status Tracking deployed → /training-status/dashboard/")

    # ── Userbase Automation → /userbase-automation/  (contents of web/). ──
    #    Browser-only app; index.html references styles.css/app.js/pipeline/vendor
    #    relatively, so the whole web/ tree maps 1:1 under the subpath.
    _copy_tree(USERBASE_SRC / "web", USERBASE_WEB)
    ok("Userbase Automation deployed → /userbase-automation/")

    # ── Panorays/Intel 471/Censys wireframe → /panorays-intel471-censys/ ──
    #    Self-contained static app (index.html + support.js runtime).
    _copy_items(PANORAYS_SRC, PANORAYS_WEB, ["index.html", "support.js"])
    ok("Panorays/Intel 471/Censys wireframe deployed → /panorays-intel471-censys/")

    # Ensure every ancestor of WEB_ROOT is traversable by nginx (world +x).
    for parent in reversed(WEB_ROOT.parents):
        if parent == Path("/"):
            continue
        try:
            mode = parent.stat().st_mode & 0o777
            if mode & 0o001 == 0:
                parent.chmod(mode | 0o001)
                ok(f"Fixed traverse permission on {parent}")
        except PermissionError:
            pass

    # Ownership + permissions.
    run(["chown", "-R", "nginx:nginx", str(WEB_ROOT)])
    run(["chmod", "-R", "u=rwX,go=rX", str(WEB_ROOT)])
    ok(f"Files deployed to {WEB_ROOT}")

# ─────────────────────────────────────────────────────────────────────────────
# Step 6 — Gophish Support backend (Flask → gunicorn → systemd)
# ─────────────────────────────────────────────────────────────────────────────
# Files/dirs that must NOT be copied into /opt (machine-specific or dev-only).
_GOPHISH_EXCLUDE = {"venv", "__pycache__", "tests", ".git", ".pytest_cache"}


def _copy_gophish_code() -> None:
    """Mirror the app source into GOPHISH_APP_DIR, excluding dev/venv cruft."""
    GOPHISH_APP_DIR.mkdir(parents=True, exist_ok=True)
    if shutil.which("rsync"):
        args = ["rsync", "-a", "--delete"]
        for name in _GOPHISH_EXCLUDE:
            args += ["--exclude", name]
        run(args + [f"{GOPHISH_SRC}/", f"{GOPHISH_APP_DIR}/"])
    else:
        ignore = shutil.ignore_patterns(*_GOPHISH_EXCLUDE)
        for item in GOPHISH_SRC.iterdir():
            if item.name in _GOPHISH_EXCLUDE:
                continue
            dst = GOPHISH_APP_DIR / item.name
            if item.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(item, dst, ignore=ignore)
            else:
                shutil.copy2(item, dst)


def _build_gophish_venv() -> bool:
    """Create the venv and install requirements (incl. gunicorn). False on failure."""
    py = shutil.which("python3") or sys.executable
    if not GOPHISH_VENV.exists():
        info("Creating Python venv for Gophish Support …")
        r = run([py, "-m", "venv", str(GOPHISH_VENV)], capture=True, check=False)
        if r.returncode != 0:
            warn("venv creation failed — is python3 + venv installed? Skipping backend.")
            if r.stderr:
                print(r.stderr[-1000:])
            return False
    venv_py = GOPHISH_VENV / "bin" / "python"
    info("Installing Python dependencies (Flask, gunicorn, …) …")
    run([str(venv_py), "-m", "pip", "install", "--upgrade", "pip"], capture=True, check=False)
    r = run(
        [str(venv_py), "-m", "pip", "install", "-r", str(GOPHISH_APP_DIR / "requirements.txt")],
        capture=True,
        check=False,
    )
    if r.returncode != 0:
        warn("pip install failed for Gophish Support — backend will not start.")
        if r.stderr:
            print(r.stderr[-1500:])
        return False
    # gunicorn is pinned in requirements.txt, but install explicitly as a safety net.
    run([str(venv_py), "-m", "pip", "install", "gunicorn"], capture=True, check=False)
    ok("Gophish Support dependencies installed")
    return True


def _gophish_secret_key() -> str:
    """Reuse the FLASK_SECRET_KEY from an existing unit file, else generate one.

    Keeps re-runs idempotent — regenerating would invalidate active sessions.
    """
    if GOPHISH_SERVICE.exists():
        for line in GOPHISH_SERVICE.read_text().splitlines():
            if "FLASK_SECRET_KEY=" in line:
                return line.split("FLASK_SECRET_KEY=", 1)[1].strip().strip('"')
    return secrets.token_hex(32)


def _write_gophish_service() -> None:
    secret = _gophish_secret_key()
    gunicorn = GOPHISH_VENV / "bin" / "gunicorn"
    unit = f"""\
# Gophish Support — bulk campaign/userbase manager (generated by deploy.py)
[Unit]
Description=Gophish Support (bulk manager) Flask app
After=network.target

[Service]
Type=simple
User=nginx
Group=nginx
WorkingDirectory={GOPHISH_APP_DIR}
Environment=FLASK_SECRET_KEY={secret}
# --timeout 900: bulk CSV import/verify can run up to ~10 minutes synchronously.
ExecStart={gunicorn} --workers 2 --timeout 900 --bind {GOPHISH_BIND} gophish_manager:app
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
"""
    GOPHISH_SERVICE.write_text(unit)
    ok(f"systemd unit written: {GOPHISH_SERVICE}")


def deploy_gophish_backend() -> bool:
    """Deploy + (re)start the Gophish Support service. Returns True if healthy."""
    info(f"Deploying Gophish Support code → {GOPHISH_APP_DIR} …")
    _copy_gophish_code()

    if not _build_gophish_venv():
        return False

    # nginx user owns the tree so the app can persist .env credentials from the UI.
    run(["chown", "-R", "nginx:nginx", str(GOPHISH_APP_DIR)])
    run(["chmod", "-R", "u=rwX,go=rX", str(GOPHISH_APP_DIR)])

    _write_gophish_service()

    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "gophish-support"], capture=True, check=False)
    run(["systemctl", "restart", "gophish-support"], check=False)

    # Give gunicorn a moment, then confirm it bound the port.
    for _ in range(6):
        r = run(["systemctl", "is-active", "gophish-support"], capture=True, check=False)
        if r.stdout.strip() == "active":
            ok(f"Gophish Support service running ({GOPHISH_BIND})")
            return True
        time.sleep(1)
    warn("Gophish Support service did not become active.")
    warn("Check:  journalctl -u gophish-support -n 50")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Step 6b — Poster app backend (Node/Express → systemd)
# ─────────────────────────────────────────────────────────────────────────────
# Files/dirs that must NOT be copied into /opt (deps, dev, or runtime state).
_POSTER_EXCLUDE = {"node_modules", ".git", "tests", ".playwright-mcp"}


def _copy_poster_code() -> None:
    """Mirror poster-app source into POSTER_APP_DIR, excluding deps/dev/runtime.

    Runtime state (the SQLite db + session token) lives under data/ and is
    regenerated on first run — never copy the local dev copies over.
    """
    POSTER_APP_DIR.mkdir(parents=True, exist_ok=True)
    if shutil.which("rsync"):
        args = ["rsync", "-a", "--delete"]
        for name in _POSTER_EXCLUDE:
            args += ["--exclude", name]
        # runtime state under data/ (keep data/article-seed.js and other source)
        for pat in ("data/*.sqlite", "data/*.sqlite-shm", "data/*.sqlite-wal",
                    "data/session-token", "image-library/assets"):
            args += ["--exclude", pat]
        run(args + [f"{POSTER_SRC}/", f"{POSTER_APP_DIR}/"])
    else:
        ignore = shutil.ignore_patterns(*_POSTER_EXCLUDE,
                                        "*.sqlite", "*.sqlite-shm", "*.sqlite-wal",
                                        "session-token")
        for item in POSTER_SRC.iterdir():
            if item.name in _POSTER_EXCLUDE:
                continue
            dst = POSTER_APP_DIR / item.name
            if item.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(item, dst, ignore=ignore)
            else:
                shutil.copy2(item, dst)


def _seed_poster_data() -> None:
    """Seed the committed library DB + image assets into /opt on a FRESH deploy.

    The running service writes the live library to /opt/poster-app/data/*.sqlite
    and /opt/poster-app/image-library/assets/, so _copy_poster_code() deliberately
    excludes those paths (redeploys never clobber generated posters). Here we copy
    the git-tracked snapshot in ONLY when /opt doesn't have one yet — so a new VM
    starts with the pre-generated library, an existing one keeps its live data.
    """
    src_db = POSTER_SRC / "data" / "poster-app.sqlite"
    dst_db = POSTER_APP_DIR / "data" / "poster-app.sqlite"
    if src_db.exists() and not dst_db.exists():
        dst_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_db, dst_db)
        ok("Seeded poster library DB from repo (data/poster-app.sqlite)")

    src_assets = POSTER_SRC / "image-library" / "assets"
    dst_assets = POSTER_APP_DIR / "image-library" / "assets"
    if src_assets.is_dir() and not dst_assets.exists():
        shutil.copytree(src_assets, dst_assets)
        n = sum(1 for _ in dst_assets.rglob("*") if _.is_file())
        ok(f"Seeded poster image assets from repo ({n} files)")
    else:
        dst_assets.mkdir(parents=True, exist_ok=True)


def _write_poster_service(node_bin: str) -> None:
    unit = f"""\
# Poster app — AI security-awareness poster generator (generated by deploy.py)
[Unit]
Description=Poster app (AI awareness poster generator)
After=network.target

[Service]
Type=simple
User=nginx
Group=nginx
WorkingDirectory={POSTER_APP_DIR}
Environment=NODE_ENV=production
Environment=POSTER_APP_PORT={POSTER_PORT}
ExecStart={node_bin} backend/server.js
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
"""
    POSTER_SERVICE.write_text(unit)
    ok(f"systemd unit written: {POSTER_SERVICE}")


def deploy_poster_backend(node_ok: bool) -> bool:
    """Deploy + (re)start the poster-app Node service. Returns True if healthy."""
    if not node_ok:
        warn("Node.js unavailable — skipping poster-app backend.")
        return False

    node_bin = shutil.which("node")
    if not node_bin:
        warn("node not found on PATH — skipping poster-app backend.")
        return False

    info(f"Deploying poster-app code → {POSTER_APP_DIR} …")
    _copy_poster_code()
    # Seed the pre-generated library on a fresh install; preserve live data on redeploy.
    _seed_poster_data()

    info("Installing poster-app dependencies (npm) — includes native better-sqlite3 …")
    r = run(["npm", "install", "--omit=dev", "--no-audit", "--no-fund"],
            cwd=POSTER_APP_DIR, capture=True, check=False)
    if r.returncode != 0:
        # retry without --omit=dev in case a runtime dep is misclassified
        r2 = run(["npm", "install", "--no-audit", "--no-fund"],
                 cwd=POSTER_APP_DIR, capture=True, check=False)
        if r2.returncode != 0:
            warn("npm install failed for poster-app — backend will not start.")
            if r2.stderr:
                print(r2.stderr[-1500:])
            warn("If offline, better-sqlite3 needs a prebuilt binary or gcc/make/python3.")
            return False
    ok("poster-app dependencies installed")

    # nginx user owns the tree (writes data/ SQLite db + session token at runtime).
    run(["chown", "-R", "nginx:nginx", str(POSTER_APP_DIR)])
    run(["chmod", "-R", "u=rwX,go=rX", str(POSTER_APP_DIR)])

    _write_poster_service(node_bin)

    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "poster-app"], capture=True, check=False)
    run(["systemctl", "restart", "poster-app"], check=False)

    for _ in range(8):
        r = run(["systemctl", "is-active", "poster-app"], capture=True, check=False)
        if r.stdout.strip() == "active":
            ok(f"Poster app service running ({POSTER_BIND})")
            return True
        time.sleep(1)
    warn("Poster app service did not become active.")
    warn("Check:  journalctl -u poster-app -n 50")
    return False


def _poster_token() -> str | None:
    """Read the generated session token (for the tokenized first-visit URL)."""
    tok = POSTER_APP_DIR / "data" / "session-token"
    try:
        return tok.read_text().strip()
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Step 7 — nginx config
# ─────────────────────────────────────────────────────────────────────────────
def _server_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))   # non-routable; no traffic sent
            return s.getsockname()[0]
    except Exception:
        return "_"

def _newsletter_source_deny() -> str:
    """Extra deny rules when the Newsletter is served from source (not dist/)."""
    return """\
    # Newsletter served from source — hide dev files.
    location ~* ^/newsletter/(tests|scripts|docs|node_modules|nessus_advisory|experiments|article-seed|ensemble-logs|playwright-report|test-results|deploy|templates/reference|templates/imported-standalone)/ {
        deny all;
    }
    location ~* ^/newsletter/(package(-lock)?\\.json|eslint\\.config\\.js|playwright\\.config\\.js|babel\\.json|.*\\.md|\\.nvmrc)$ {
        deny all;
    }
"""

# CSP relaxed enough for all three apps:
#   • Newsletter uses inline scripts + cdnjs.
#   • PRP Charts loads Plotly/xlsx/exceljs (inline + blob workers) with CDN fallback.
#   • Training runs in-browser xlsx parsing (inline + blob workers).
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' blob: https://cdnjs.cloudflare.com https://cdn.plot.ly https://cdn.jsdelivr.net; "
    "worker-src 'self' blob:; "
    "child-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "img-src 'self' data: blob: https:; "
    "connect-src 'self' blob: https:; "
    "frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"
)

_NGINX_TEMPLATE = """\
# Security & Risk Portal — nginx config (generated by deploy.py)
# Re-run deploy.py to regenerate.

server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name @@SERVER_NAME@@ _;

    root @@WEB_ROOT@@;
    index index.html;

    server_tokens off;
    client_max_body_size 32m;   # PRP/Training accept user Excel uploads (client-side)

    # ── Security headers ─────────────────────────────────────────────────────
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Permissions-Policy "geolocation=(), microphone=(), camera=(), payment=(), usb=()" always;
    add_header Content-Security-Policy "@@CSP@@" always;

    # ── Compression ──────────────────────────────────────────────────────────
    gzip on;
    gzip_vary on;
    gzip_min_length 1024;
    gzip_proxied any;
    gzip_comp_level 6;
    gzip_types text/plain text/css text/javascript application/javascript application/json application/wasm image/svg+xml application/xml;

    # ── Health endpoint ───────────────────────────────────────────────────────
    location = /health.html {
        access_log off;
        add_header Cache-Control "no-store" always;
    }

    # ── Deny hidden files ─────────────────────────────────────────────────────
    location ~ /\\.  { deny all; }

@@NEWSLETTER_DENY@@
    # ── Newsletter (Awareness) — SPA ──────────────────────────────────────────
    location /newsletter/ {
        try_files $uri $uri/ /newsletter/index.html;
    }

    # ── PRP Charts (TPRM) — static app ────────────────────────────────────────
    location /prp-charts/ {
        try_files $uri $uri/ =404;
    }

    # ── Panorays/Intel 471/Censys wireframe (TPRM) — static app ───────────────
    location /panorays-intel471-censys/ {
        try_files $uri $uri/ =404;
    }

    # ── Training Status Tracking (Awareness) — entry lives in dashboard/ ──────
    location = /training-status/ { return 301 /training-status/dashboard/; }
    location = /training-status  { return 301 /training-status/dashboard/; }
    location /training-status/ {
        try_files $uri $uri/ =404;
    }

    # ── Gophish Support (Awareness) — Flask backend via reverse proxy ─────────
    #    ^~ so nginx does NOT fall through to the regex cache locations below
    #    (proxied assets like /static/*.png live in the app, not on disk).
    location = /gophish-support { return 301 /gophish-support/; }
    location ^~ /gophish-support/ {
        proxy_pass http://@@GOPHISH_BIND@@/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Prefix /gophish-support;
        # Bulk CSV import/verify can take up to ~10 minutes.
        proxy_read_timeout 900s;
        proxy_send_timeout 900s;
    }

    # ── Userbase Automation (Awareness) — browser-only static app ─────────────
    location = /userbase-automation { return 301 /userbase-automation/; }
    location /userbase-automation/ {
        try_files $uri $uri/ =404;
    }

    # ── Poster app (Awareness) — Node service via reverse proxy ───────────────
    #    UI at /poster-app/ (relative assets); its browser code calls absolute
    #    /api/… so /api/ is proxied to the same backend (incl. the SSE stream).
    #    Token still required on first visit: /poster-app/?token=<session-token>.
    location = /poster-app { return 301 /poster-app/; }
    location ^~ /poster-app/ {
        proxy_pass http://@@POSTER_BIND@@/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 900s;
        proxy_send_timeout 900s;
    }
    location ^~ /api/ {
        proxy_pass http://@@POSTER_BIND@@;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_buffering off;                # /api/events/stream is Server-Sent Events
        proxy_read_timeout 3600s;           # pipeline runs are minutes of model calls
        proxy_send_timeout 3600s;
    }

    # ── Cache by file type ────────────────────────────────────────────────────
    location ~* \\.html?$ {
        add_header Cache-Control "public, max-age=0, must-revalidate" always;
    }
    location ~* \\.(js|css|wasm)$ {
        add_header Cache-Control "public, max-age=86400, must-revalidate" always;
    }
    location ~* \\.(jpg|jpeg|png|gif|webp|ico|svg|woff|woff2|ttf|eot)$ {
        add_header Cache-Control "public, max-age=604800" always;
    }

    # ── Home launcher / SPA-safe fallback ─────────────────────────────────────
    location / {
        try_files $uri $uri/ /index.html;
    }
}
"""

def _strip_other_default_servers() -> None:
    """Ensure our portal.conf is the ONLY 'default_server' on :80.

    Stock RHEL nginx ships a default server block inside /etc/nginx/nginx.conf
    (and sometimes other conf.d files). Two 'default_server' directives on the
    same address:port make `nginx -t` fail with:
        a duplicate default server for 0.0.0.0:80
    Strip the keyword from every OTHER config (backup kept as *.bak), leaving
    ours untouched.
    """
    targets: list[Path] = []
    main = Path("/etc/nginx/nginx.conf")
    if main.exists():
        targets.append(main)
    confd = Path("/etc/nginx/conf.d")
    if confd.is_dir():
        for f in sorted(confd.glob("*.conf")):
            if f != NGINX_CONF:
                targets.append(f)

    for f in targets:
        try:
            text = f.read_text()
        except Exception:
            continue
        if "default_server" not in text:
            continue
        backup = f.with_name(f.name + ".bak")
        if not backup.exists():
            backup.write_text(text)
        new = text.replace(" default_server", "")
        if new != text:
            f.write_text(new)
            info(f"Removed default_server from {f} (backup: {backup.name})")


def configure_nginx(newsletter_is_dist: bool) -> None:
    server_ip = _server_ip()
    deny = "" if newsletter_is_dist else _newsletter_source_deny()
    conf = (
        _NGINX_TEMPLATE
        .replace("@@SERVER_NAME@@", server_ip)
        .replace("@@WEB_ROOT@@", str(WEB_ROOT))
        .replace("@@CSP@@", _CSP)
        .replace("@@GOPHISH_BIND@@", GOPHISH_BIND)
        .replace("@@POSTER_BIND@@", POSTER_BIND)
        .replace("@@NEWSLETTER_DENY@@\n", (deny + "\n") if deny else "")
    )

    # Disable the default catch-all config (conflicts with default_server).
    if NGINX_DEFAULT.exists():
        NGINX_DEFAULT.rename(NGINX_DEFAULT.with_suffix(".conf.disabled"))
        info("Disabled /etc/nginx/conf.d/default.conf (renamed .disabled)")

    NGINX_CONF.write_text(conf)
    ok(f"nginx config written: {NGINX_CONF}")

    # Make ours the only default_server on :80 (strip the keyword elsewhere).
    _strip_other_default_servers()

    r = run(["nginx", "-t"], capture=True, check=False)
    if r.returncode != 0:
        err("nginx -t failed:")
        print(r.stderr)
        die("Fix the nginx config error above and re-run deploy.py.")
    ok("nginx config syntax OK")

# ─────────────────────────────────────────────────────────────────────────────
# Step 8 — SELinux
# ─────────────────────────────────────────────────────────────────────────────
def fix_selinux() -> None:
    r = run(["getenforce"], capture=True, check=False)
    if r.returncode != 0:
        info("getenforce not found — SELinux step skipped.")
        return
    mode = r.stdout.strip().lower()
    if mode == "disabled":
        info("SELinux disabled — skipping context fix.")
        return

    info(f"SELinux mode: {mode}. Setting httpd_sys_content_t on {WEB_ROOT} …")
    run(["chcon", "-R", "-t", "httpd_sys_content_t", str(WEB_ROOT)], check=False)
    run(["restorecon", "-Rv", str(WEB_ROOT)], capture=True, check=False)
    ok("SELinux context set (httpd_sys_content_t)")

    # Allow nginx to open a connection to the gunicorn backend (reverse proxy).
    if shutil.which("setsebool"):
        info("Allowing nginx→backend connections (httpd_can_network_connect) …")
        run(["setsebool", "-P", "httpd_can_network_connect", "1"], check=False)
        ok("SELinux: httpd_can_network_connect enabled")

# ─────────────────────────────────────────────────────────────────────────────
# Step 9 — Firewall
# ─────────────────────────────────────────────────────────────────────────────
def configure_firewall() -> None:
    if not shutil.which("firewall-cmd"):
        warn("firewall-cmd not found — skipping firewall config.")
        return

    r = run(["systemctl", "is-active", "firewalld"], capture=True, check=False)
    if r.stdout.strip() != "active":
        warn("firewalld is not running — skipping firewall config.")
        return

    info("Opening HTTP (port 80) in firewalld …")
    run(["firewall-cmd", "--permanent", "--add-service=http"])
    run(["firewall-cmd", "--reload"])
    ok("firewalld: port 80 open")

# ─────────────────────────────────────────────────────────────────────────────
# Step 10 — Start nginx + health check
# ─────────────────────────────────────────────────────────────────────────────
def start_nginx() -> None:
    run(["systemctl", "enable", "nginx"])

    r = run(["systemctl", "is-active", "nginx"], capture=True, check=False)
    if r.stdout.strip() == "active":
        run(["systemctl", "reload", "nginx"])
        ok("nginx reloaded")
    else:
        run(["systemctl", "start", "nginx"])
        ok("nginx started and enabled")

def health_check() -> None:
    url = "http://127.0.0.1/health.html"
    info(f"Health check → {url}")
    for attempt in range(6):
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    ok(f"Health check passed (HTTP {resp.status})")
                    return
                warn(f"Unexpected HTTP {resp.status} from {url}")
                return
        except Exception as exc:
            if attempt < 5:
                time.sleep(1)
            else:
                warn(f"Health check failed after 6 tries: {exc}")
                warn("Check:  journalctl -u nginx -n 50")

# ─────────────────────────────────────────────────────────────────────────────
# Success summary
# ─────────────────────────────────────────────────────────────────────────────
def print_success(newsletter_is_dist: bool, gophish_ok: bool, poster_ok: bool) -> None:
    ip = _server_ip()
    label = "dist/ (production build)" if newsletter_is_dist else "source (Node.js was unavailable)"
    gophish_note = "" if gophish_ok else _c("1;33", "  (service not active — see journalctl -u gophish-support)")
    poster_note = "" if poster_ok else _c("1;33", "  (service not active — see journalctl -u poster-app)")

    # Poster app is token-gated — surface the tokenized first-visit URL.
    poster_token_line = ""
    if poster_ok:
        tok = _poster_token()
        if tok:
            poster_token_line = (
                f"\n  Poster app — open ONCE to set the session cookie:\n"
                f"    {_c('1;33', f'http://{ip}/poster-app/?token={tok}')}\n"
                f"    (then configure an OpenAI API key in the Config tab to generate)"
            )

    banner("Deployment complete")
    print(f"""
  Newsletter served from: {_c('1', label)}

  Home launcher:
    Local    →  {_c('1;36', 'http://127.0.0.1/')}
    Network  →  {_c('1;36', f'http://{ip}/')}

  Tools:
    Newsletter                →  {_c('1;36', f'http://{ip}/newsletter/')}
    Training Status Tracking  →  {_c('1;36', f'http://{ip}/training-status/dashboard/')}
    Gophish Support           →  {_c('1;36', f'http://{ip}/gophish-support/')}{gophish_note}
    Userbase Automation       →  {_c('1;36', f'http://{ip}/userbase-automation/')}
    Poster app                →  {_c('1;36', f'http://{ip}/poster-app/')}{poster_note}
    PRP Charts                →  {_c('1;36', f'http://{ip}/prp-charts/')}
    Panorays/Intel471/Censys  →  {_c('1;36', f'http://{ip}/panorays-intel471-censys/')}
{poster_token_line}

  Useful commands:
    systemctl status nginx
    journalctl -u nginx -f
    systemctl reload nginx
    systemctl status gophish-support
    systemctl status poster-app
    journalctl -u poster-app -f

  Files:
    Web root  {WEB_ROOT}
    Config    {NGINX_CONF}
    Gophish   {GOPHISH_APP_DIR}  (service: gophish-support)
    Poster    {POSTER_APP_DIR}  (service: poster-app)
""")

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    TOTAL = 11
    banner("Security & Risk Portal — RHEL 9.8 Deployment Script")

    step(1, TOTAL, "Preflight checks")
    check_root()
    check_rhel()
    check_project()

    step(2, TOTAL, "Install Node.js 20")
    node_ok = install_nodejs()

    step(3, TOTAL, "Install nginx")
    install_nginx()

    step(4, TOTAL, "Build Newsletter production artifact (dist/)")
    if node_ok:
        built = build_newsletter()
    else:
        warn("Node.js unavailable — skipping Newsletter build.")
        built = False

    newsletter_is_dist = bool(built and NEWSLETTER_DIST.is_dir())
    if newsletter_is_dist:
        info("Newsletter: using dist/ (clean production artifact)")
    else:
        warn("Newsletter: falling back to source directory")

    step(5, TOTAL, "Deploy home page + static apps to web root")
    deploy_files(newsletter_is_dist)

    step(6, TOTAL, "Deploy Gophish Support backend (gunicorn + systemd)")
    gophish_ok = deploy_gophish_backend()

    step(7, TOTAL, "Deploy Poster app backend (Node + systemd)")
    poster_ok = deploy_poster_backend(node_ok)

    step(8, TOTAL, "Configure nginx")
    configure_nginx(newsletter_is_dist)

    step(9, TOTAL, "Fix SELinux file context")
    fix_selinux()

    step(10, TOTAL, "Configure firewall")
    configure_firewall()

    step(11, TOTAL, "Start nginx + health check")
    start_nginx()
    health_check()

    print_success(newsletter_is_dist, gophish_ok, poster_ok)


if __name__ == "__main__":
    main()
