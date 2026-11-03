"""Getting the GitHub CLI and signing in without a terminal (jobradar/ghcli.py)."""

import hashlib
import io
import sys
import tarfile
import time
import zipfile

import pytest

from jobradar import ghcli


def test_asset_suffix_per_system():
    assert ghcli.asset_suffix("Darwin", "arm64") == "_macOS_arm64.zip"
    assert ghcli.asset_suffix("Darwin", "x86_64") == "_macOS_amd64.zip"
    assert ghcli.asset_suffix("Windows", "AMD64") == "_windows_amd64.zip"
    assert ghcli.asset_suffix("Linux", "aarch64") == "_linux_arm64.tar.gz"


def _zip(member: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("gh_9.9.9_macOS_arm64/LICENSE", "x")
        z.writestr(member, data)
    return buf.getvalue()


def _tgz(member: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        info = tarfile.TarInfo(member)
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_extract_finds_the_binary():
    assert ghcli._extract("gh_9.9.9_macOS_arm64.zip", _zip("gh_9.9.9_macOS_arm64/bin/gh", b"BIN")) == b"BIN"
    assert ghcli._extract("gh_9.9.9_windows_amd64.zip", _zip("bin/gh.exe", b"EXE")) == b"EXE"
    assert ghcli._extract("gh_9.9.9_linux_amd64.tar.gz", _tgz("gh_9.9.9_linux_amd64/bin/gh", b"ELF")) == b"ELF"


class FakeHTTP:
    def __init__(self, files):
        self.files = files

    def get(self, url):
        body = self.files[url]
        return type("R", (), {"json": lambda s: body, "text": body if isinstance(body, str) else "",
                              "content": body if isinstance(body, bytes) else b""})()


def _release(blob: bytes, digest: str | None = None):
    name = "gh_9.9.9" + ghcli.asset_suffix()
    sums = f"{digest or hashlib.sha256(blob).hexdigest()}  {name}\n0000  gh_9.9.9_other.zip\n"
    return FakeHTTP({ghcli.RELEASE_API: {"tag_name": "v9.9.9", "assets": [
        {"name": name, "browser_download_url": "u/bin"}, {"name": "gh_9.9.9_checksums.txt", "browser_download_url": "u/sums"}]},
        "u/bin": blob, "u/sums": sums})


def test_install_verifies_the_checksum(monkeypatch, tmp_path):
    monkeypatch.setenv("JR_HOME", str(tmp_path))
    name = "gh_9.9.9" + ghcli.asset_suffix()
    blob = _zip("bin/gh", b"BIN") if name.endswith(".zip") else _tgz("x/bin/gh", b"BIN")
    bad = ghcli.install(_release(blob, digest="0" * 64))
    assert not bad["ok"] and "checksum" in bad["error"] and ghcli.local_gh() is None
    good = ghcli.install(_release(blob))
    assert good["ok"] and good["version"] == "v9.9.9" and ghcli.local_gh().read_bytes() == b"BIN"


@pytest.mark.skipif(sys.platform == "win32", reason="fake gh is a shell-less Python script")
def test_login_shows_the_code_then_finishes(monkeypatch, tmp_path):
    fake = tmp_path / "gh"
    fake.write_text(f"#!{sys.executable}\nimport sys, time\nprint('! One-time code (AB12-CD34) copied to clipboard')\n"
                    "print('Open this URL to continue in your web browser: https://github.com/login/device')\n"
                    "sys.stdout.flush(); time.sleep(0.5)\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("JR_GH", str(fake))
    st = ghcli.start_login(wait_s=5)
    assert st["ok"] and st["code"] == "AB12-CD34" and st["url"] == "https://github.com/login/device"
    for _ in range(50):
        if ghcli.login_state().get("done"):
            break
        time.sleep(0.1)
    assert ghcli.login_state()["done"] is True
