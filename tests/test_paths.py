"""Cross-platform path resolution, Windows included."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from jaigent import paths
from jaigent.paths import project_home, scoped_dirs, user_file, user_home


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("JAIGENT_HOME", "APPDATA", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)


class TestUserHome:
    def test_override_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "custom"))
        assert user_home() == tmp_path / "custom"

    def test_override_expands_a_tilde(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JAIGENT_HOME", "~/somewhere")
        assert "~" not in str(user_home())

    def test_windows_uses_appdata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", r"C:\Users\jaime\AppData\Roaming")

        resolved = str(user_home())
        assert "AppData" in resolved
        assert resolved.endswith("jaigent")

    def test_windows_without_appdata_falls_back_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        assert user_home().name == "jaigent"

    def test_xdg_is_honoured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

        assert user_home() == tmp_path / "cfg" / "jaigent"

    def test_plain_unix_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        assert user_home() == Path.home() / ".jaigent"

    def test_override_beats_appdata(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", r"C:\AppData")
        monkeypatch.setenv("JAIGENT_HOME", str(tmp_path))

        assert user_home() == tmp_path


class TestProjectHome:
    def test_uses_a_dot_directory_on_every_platform(self, tmp_path: Path) -> None:
        # A project directory is committed, so it must not vary by OS.
        assert project_home(tmp_path) == tmp_path / ".jaigent"

    def test_defaults_to_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert project_home() == tmp_path / ".jaigent"


class TestHelpers:
    def test_user_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JAIGENT_HOME", str(tmp_path))
        assert user_file("settings.json") == tmp_path / "settings.json"

    def test_scoped_dirs_order(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "home"))
        scopes = scoped_dirs("skills", tmp_path / "proj")

        # User first so project definitions overwrite them during discovery.
        assert [name for name, _ in scopes] == ["user", "project"]
        assert scopes[0][1] == tmp_path / "home" / "skills"
        assert scopes[1][1] == tmp_path / "proj" / ".jaigent" / "skills"

    def test_is_windows_matches_the_platform(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        assert paths.is_windows() is True
        monkeypatch.setattr(sys, "platform", "darwin")
        assert paths.is_windows() is False


class TestConsumersAgree:
    """Every store must resolve under the same root."""

    def test_all_stores_share_the_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "h"))
        for var in ("JAIGENT_SCHEDULE_FILE", "JAIGENT_KEYS_FILE", "JAIGENT_SESSION_DIR"):
            monkeypatch.delenv(var, raising=False)

        from jaigent import gateway, schedule, session, settings_store
        from jaigent.commands import commands_dirs
        from jaigent.skills import skills_dirs

        root = tmp_path / "h"
        assert settings_store.user_settings_path().parent == root
        assert schedule.schedules_path().parent == root
        assert gateway.keys_path().parent == root
        assert session.session_dir().parent == root
        assert dict(skills_dirs())["user"].parent == root
        assert dict(commands_dirs())["user"].parent == root


class TestWritePrivate:
    """Files holding credentials must not be readable by other users."""

    def test_content_roundtrips(self, tmp_path: Path) -> None:
        target = paths.write_private(tmp_path / ".env", "OPENAI_API_KEY=sk-secret")

        assert target.read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-secret"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
    def test_the_file_is_owner_only(self, tmp_path: Path) -> None:
        target = paths.write_private(tmp_path / ".env", "secret")

        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
    def test_an_existing_world_readable_file_is_tightened(self, tmp_path: Path) -> None:
        target = tmp_path / ".env"
        target.write_text("old")
        target.chmod(0o644)

        paths.write_private(target, "new")

        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_parent_directories_are_created(self, tmp_path: Path) -> None:
        target = paths.write_private(tmp_path / "deep" / "nested" / ".env", "secret")

        assert target.is_file()

    def test_no_temporary_file_is_left_behind(self, tmp_path: Path) -> None:
        paths.write_private(tmp_path / ".env", "secret")

        assert [p.name for p in tmp_path.iterdir()] == [".env"]

    def test_a_replaced_file_keeps_no_old_content(self, tmp_path: Path) -> None:
        target = tmp_path / ".env"
        paths.write_private(target, "first value that is quite long")
        paths.write_private(target, "short")

        assert target.read_text(encoding="utf-8") == "short"
