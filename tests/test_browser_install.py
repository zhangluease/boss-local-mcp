from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

import browser_install as installer


@pytest.mark.parametrize("system,machine,expected", [
    ("darwin", "arm64", "mac-arm64"), ("darwin", "x86_64", "mac-x64"),
    ("win32", "AMD64", "win64"), ("win32", "x86", "win32"),
    ("linux", "x86_64", "linux64"), ("linux", "aarch64", "linux-arm64"),
])
def test_select_official_archive_for_host(system, machine, expected):
    assert installer.browser_platform(system, machine) == expected


def test_unsupported_platform_is_explicit():
    with pytest.raises(installer.BrowserInstallError, match="尚不支持"):
        installer.browser_platform("win32", "ARM64")


@pytest.fixture
def installation(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_BROWSER_DIR", str(tmp_path / "browsers"))
    platform_name = installer.browser_platform()
    archive = tmp_path / "official-fixture.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(installer.MANIFEST["executables"][platform_name], installer.CHROME_VERSION)
    downloads = []

    def download(url, destination, progress):
        downloads.append(url)
        shutil.copyfile(archive, destination)

    monkeypatch.setattr(installer, "_download", download)
    monkeypatch.setattr(installer, "executable_version", lambda path: Path(path).read_text())
    return SimpleNamespace(root=tmp_path / "browsers", archive=archive, downloads=downloads)


def test_install_is_project_local_atomic_and_reusable(installation):
    result = installer.install_browser()
    assert result["version"] == installer.CHROME_VERSION
    assert Path(result["path"]).is_relative_to(installation.root)
    assert Path(result["path"]).read_text() == installer.CHROME_VERSION
    assert installer.install_browser()["status"] == "already_installed"
    assert len(installation.downloads) == 1
    assert installation.downloads[0].startswith("https://storage.googleapis.com/chrome-for-testing-public/")
    assert not list(installation.root.glob(".install*"))
    assert not list(installation.root.rglob("*.zip"))


@pytest.mark.parametrize("failure", ["invalid_zip", "wrong_version", "traversal"])
def test_failed_install_does_not_activate_or_leave_archives(installation, failure):
    if failure == "invalid_zip":
        installation.archive.write_text("incomplete download")
    else:
        with zipfile.ZipFile(installation.archive, "w") as package:
            path = "../../outside" if failure == "traversal" else installer.MANIFEST["executables"][installer.browser_platform()]
            package.writestr(path, "1.2.3.4")
    with pytest.raises((installer.BrowserInstallError, zipfile.BadZipFile)):
        installer.install_browser()
    assert not installer.managed_executable().exists()
    assert list(installation.root.iterdir()) == []
    assert not (installation.root.parent / "outside").exists()


def test_install_lock_blocks_a_second_installer(installation):
    installation.root.mkdir()
    lock = installation.root / f".install-{installer.CHROME_VERSION}-{installer.browser_platform()}.lock"
    lock.touch()
    with pytest.raises(installer.BrowserInstallError, match="已有浏览器安装任务"):
        installer.install_browser()
    assert installation.downloads == []
    assert lock.exists()


def test_paths_are_anchored_to_project_not_host_working_directory(tmp_path, monkeypatch):
    project, host = tmp_path / "project", tmp_path / "host"
    project.mkdir()
    host.mkdir()
    monkeypatch.setattr(installer, "ROOT", project)
    monkeypatch.setenv("BOSS_BROWSER_DIR", ".browsers")
    monkeypatch.delenv("BOSS_CHROME_PATH", raising=False)
    monkeypatch.chdir(host)
    assert installer.configured_executable().is_relative_to(project / ".browsers")
    monkeypatch.setenv("BOSS_CHROME_PATH", "custom/chrome")
    assert installer.configured_executable() == project / "custom/chrome"


@pytest.mark.asyncio
async def test_wrong_version_cdp_is_rejected_before_attaching(monkeypatch):
    import browser

    class Connection:
        browser_version = "Chrome/1.2.3.4"
        closed = False

        async def close(self):
            self.closed = True

    connection = Connection()

    async def connect(endpoint):
        return connection

    monkeypatch.setattr(browser.CDPConnection, "connect", connect)
    with pytest.raises(installer.BrowserVersionError, match="实际 1.2.3.4"):
        await browser.BossBrowser()._try_cdp_connect("http://127.0.0.1:9222")
    assert connection.closed


@pytest.mark.asyncio
async def test_launcher_uses_selected_binary_and_platform_process_options(tmp_path, monkeypatch):
    import browser
    executable = tmp_path / "selected-chrome"
    executable.touch()
    monkeypatch.setattr(browser, "configured_executable", lambda: executable)
    monkeypatch.setattr(browser, "executable_version", lambda path: installer.CHROME_VERSION)
    monkeypatch.setattr(browser, "CHROME_PROFILE_DIR", str(tmp_path / "profile"))
    calls = []
    monkeypatch.setattr(browser.subprocess, "Popen", lambda arguments, **options: calls.append((arguments, options)))
    await browser.BossBrowser()._launch_system_chrome()
    arguments, options = calls[0]
    assert arguments[0] == str(executable)
    assert f"--user-data-dir={tmp_path / 'profile'}" in arguments
    assert options["stdin"] == subprocess.DEVNULL
    if sys.platform == "win32":
        assert options["creationflags"] & subprocess.DETACHED_PROCESS
        assert "start_new_session" not in options
    else:
        assert options["start_new_session"] is True


@pytest.mark.asyncio
async def test_missing_pinned_browser_does_not_fall_back_to_system_chrome(tmp_path, monkeypatch):
    import browser
    monkeypatch.delenv("BOSS_CHROME_PATH", raising=False)
    monkeypatch.setenv("BOSS_BROWSER_DIR", str(tmp_path / "empty"))
    with pytest.raises(installer.BrowserInstallError, match="install-browser"):
        await browser.BossBrowser()._launch_system_chrome()


@pytest.mark.asyncio
async def test_real_cdp_reports_the_pinned_browser_version(chrome_endpoint):
    from cdp import CDPConnection
    connection = await CDPConnection.connect(chrome_endpoint)
    try:
        assert installer.require_version(connection.browser_version) == installer.CHROME_VERSION
    finally:
        await connection.close()
