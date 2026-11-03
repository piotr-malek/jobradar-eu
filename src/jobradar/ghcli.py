"""The GitHub CLI for people who have never used a terminal: download it, and sign in from the wizard page.

* ``install()`` fetches the official release for this OS and CPU from github.com/cli/cli, checks it against the
  release's SHA-256 checksums file and puts the binary in ~/.job-radar/bin (``sync.gh_path`` looks there).
* ``start_login()`` runs ``gh auth login --web`` (or ``gh auth refresh`` for missing permissions) in the background.
  Without a terminal gh prints a one-time code and https://github.com/login/device, then waits until the user approves
  in the browser; the page shows the code and polls ``login_state()``.
"""

from __future__ import annotations

import hashlib
import io
import platform
import re
import subprocess
import tarfile
import threading
import zipfile
from pathlib import Path
from typing import Any

from jobradar.settings import local_dir

RELEASE_API = "https://api.github.com/repos/cli/cli/releases/latest"
SCOPES = "repo,workflow"


def bin_dir() -> Path:
    return local_dir() / "bin"


def local_gh() -> Path | None:
    exe = bin_dir() / ("gh.exe" if platform.system() == "Windows" else "gh")
    return exe if exe.is_file() else None


def asset_suffix(system: str | None = None, machine: str | None = None) -> str:
    """The release asset name ending for this computer, e.g. "_macOS_arm64.zip"."""
    system = system or platform.system()
    m = (machine or platform.machine()).lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "amd64"
    if system == "Darwin":
        return f"_macOS_{arch}.zip"
    if system == "Windows":
        return f"_windows_{arch}.zip"
    return f"_linux_{arch}.tar.gz"


def _extract(name: str, blob: bytes) -> bytes:
    """The gh binary from a release archive (…/bin/gh or …/bin/gh.exe)."""
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            member = next(n for n in z.namelist() if re.search(r"(^|/)bin/gh(\.exe)?$", n))
            return z.read(member)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
        member = next(m for m in t.getmembers() if re.search(r"(^|/)bin/gh$", m.name))
        f = t.extractfile(member)
        assert f is not None
        return f.read()


def install(http: Any = None) -> dict[str, Any]:
    """Download and verify the latest gh into ~/.job-radar/bin. Returns {"ok", "path", "version"} or {"ok": False, "error"}."""
    import httpx

    client = http or httpx.Client(timeout=120, follow_redirects=True, headers={"User-Agent": "jobradar-eu"})
    try:
        rel = client.get(RELEASE_API).json()
        suffix = asset_suffix()
        assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets") or []}
        name = next((n for n in assets if n.endswith(suffix)), None)
        sums_name = next((n for n in assets if n.endswith("_checksums.txt")), None)
        if not name or not sums_name:
            return {"ok": False, "error": f"no GitHub CLI download for this computer ({suffix.strip('_')})"}
        sums = dict(line.split()[::-1] for line in client.get(assets[sums_name]).text.splitlines() if line.strip())
        blob = client.get(assets[name]).content
        if hashlib.sha256(blob).hexdigest() != sums.get(name):
            return {"ok": False, "error": "the download didn't match GitHub's checksum; nothing was installed"}
        exe = bin_dir() / ("gh.exe" if name.endswith(".zip") and "windows" in name else "gh")
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(_extract(name, blob))
        exe.chmod(0o755)
        return {"ok": True, "path": str(exe), "version": rel.get("tag_name")}
    except Exception as exc:  # noqa: BLE001 - shown on the page
        return {"ok": False, "error": f"download failed ({type(exc).__name__}: {str(exc)[:200]})"}
    finally:
        if http is None:
            client.close()


# ------------------------------------------------------------------ sign in from the page
_LOCK = threading.Lock()
_LOGIN: dict[str, Any] = {}
CODE_RX = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b")
URL_RX = re.compile(r"https://github\.com/login/device\S*")


def _reader(proc: subprocess.Popen, state: dict[str, Any]) -> None:
    assert proc.stdout is not None
    for raw in iter(proc.stdout.readline, b""):
        line = raw.decode("utf-8", "replace")
        state["output"] = (state.get("output", "") + line)[-4000:]
        if not state.get("code") and (m := CODE_RX.search(line)):
            state["code"] = m.group(1)
        if not state.get("url") and (m := URL_RX.search(line)):
            state["url"] = m.group(0).rstrip(".")
    state["exit"] = proc.wait()


def start_login(refresh: bool = False, wait_s: float = 15.0) -> dict[str, Any]:
    """Start a browser sign-in (or a permission refresh) and return as soon as gh shows its one-time code."""
    from jobradar.local.sync import gh_path

    exe = gh_path()
    if not exe:
        return {"ok": False, "error": "the GitHub CLI isn't installed yet"}
    with _LOCK:
        cur = _LOGIN.get("proc")
        if cur is not None and cur.poll() is None:
            return login_state()
        args = (["auth", "refresh", "--hostname", "github.com", "--scopes", SCOPES] if refresh else
                ["auth", "login", "--web", "--hostname", "github.com", "--git-protocol", "https", "--scopes", SCOPES,
                 "--skip-ssh-key"])
        proc = subprocess.Popen([exe, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        _LOGIN.clear()
        _LOGIN["proc"] = proc
        threading.Thread(target=_reader, args=(proc, _LOGIN), daemon=True).start()
    done = threading.Event()
    for _ in range(int(wait_s * 10)):
        if _LOGIN.get("code") or "exit" in _LOGIN:
            break
        done.wait(0.1)
    return login_state()


def login_state() -> dict[str, Any]:
    st = {k: _LOGIN.get(k) for k in ("code", "url")}
    st["url"] = st["url"] or "https://github.com/login/device"
    if "exit" in _LOGIN:
        st["done"] = _LOGIN["exit"] == 0
        if not st["done"]:
            st["error"] = (_LOGIN.get("output") or "").strip().splitlines()[-1:] or ["sign-in stopped"]
            st["error"] = st["error"][0][:300]
    else:
        st["running"] = "proc" in _LOGIN
    st["ok"] = "error" not in st
    return st
