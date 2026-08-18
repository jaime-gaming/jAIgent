"""The PyInstaller spec, executed without PyInstaller.

A `.spec` file is plain Python that PyInstaller ``exec``s with a handful of
names predefined. That means it can be run here with stubs in place of the
real build classes, which is the only way to catch its bugs without a build:
the Windows executable is only ever produced on a Windows runner.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "jaigent.spec"


class Recorder:
    """Stands in for Analysis / PYZ / EXE and remembers how it was called."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        # Analysis exposes these as attributes; EXE reads them straight back.
        self.pure = "pure"
        self.zipped_data = "zipped_data"
        self.scripts = "scripts"
        self.binaries = "binaries"
        self.zipfiles = "zipfiles"
        self.datas = "datas"


def run_spec(*, platform: str = "linux", spec_path: Path | None = None) -> dict[str, Recorder]:
    """Execute the spec on a pretend platform and return what it built."""
    built: dict[str, Recorder] = {}

    def factory(name: str):  # noqa: ANN202
        def make(*args: Any, **kwargs: Any) -> Recorder:
            recorder = Recorder(*args, **kwargs)
            built[name] = recorder
            return recorder

        return make

    path = spec_path or SPEC
    namespace: dict[str, Any] = {
        "SPECPATH": str(path.parent),
        "Analysis": factory("Analysis"),
        "PYZ": factory("PYZ"),
        "EXE": factory("EXE"),
        "__builtins__": __builtins__,
    }

    real_platform = sys.platform
    try:
        sys.platform = platform  # type: ignore[misc]
        exec(compile(path.read_text(), str(path), "exec"), namespace)  # noqa: S102
    finally:
        sys.platform = real_platform  # type: ignore[misc]
    return built


class TestSpecRuns:
    def test_the_spec_exists(self) -> None:
        assert SPEC.is_file()

    @pytest.mark.parametrize("platform", ["linux", "win32", "darwin"])
    def test_it_executes_on_every_target(self, platform: str) -> None:
        built = run_spec(platform=platform)

        assert set(built) == {"Analysis", "PYZ", "EXE"}

    def test_the_executable_is_named_jaigent(self) -> None:
        # The archive and the smoke test in the release workflow both assume it.
        assert run_spec()["EXE"].kwargs["name"] == "jaigent"

    def test_it_is_a_console_application(self) -> None:
        assert run_spec()["EXE"].kwargs["console"] is True

    def test_the_launcher_is_the_entry_point(self) -> None:
        scripts = run_spec()["Analysis"].args[0]

        assert scripts == [str(ROOT / "packaging" / "launcher.py")]
        assert Path(scripts[0]).is_file()


class TestWindowsIcon:
    """PyInstaller aborts the build when it is pointed at a missing icon.

    The spec used to name ``packaging/icon.ico`` unconditionally while no such
    file was committed, so the Windows job would have failed on its first run.
    """

    def test_the_icon_is_committed(self) -> None:
        icon = ROOT / "packaging" / "icon.ico"

        assert icon.is_file(), "packaging/icon.ico is missing; the Windows build will abort"
        assert icon.stat().st_size > 0

    def test_windows_builds_use_it(self) -> None:
        assert run_spec(platform="win32")["EXE"].kwargs["icon"] == str(
            ROOT / "packaging" / "icon.ico"
        )

    @pytest.mark.parametrize("platform", ["linux", "darwin"])
    def test_other_platforms_do_not(self, platform: str) -> None:
        assert run_spec(platform=platform)["EXE"].kwargs["icon"] is None

    def test_a_missing_icon_degrades_instead_of_failing(self, tmp_path: Path) -> None:
        # Copy the spec somewhere with no icon beside it and build for Windows.
        packaging = tmp_path / "packaging"
        packaging.mkdir()
        copy = packaging / "jaigent.spec"
        copy.write_text(SPEC.read_text())

        built = run_spec(platform="win32", spec_path=copy)

        assert built["EXE"].kwargs["icon"] is None, "a missing icon must not abort the build"

    def test_the_icon_carries_the_sizes_windows_asks_for(self) -> None:
        pillow = pytest.importorskip("PIL.Image", reason="pillow is only needed to inspect the ico")
        with pillow.open(ROOT / "packaging" / "icon.ico") as image:
            sizes = set(image.info.get("sizes", set()))

        assert (16, 16) in sizes, "no 16x16: Windows will scale the taskbar icon badly"
        assert (256, 256) in sizes


class TestFrozenImports:
    """Modules the freezer cannot see statically must be listed by hand."""

    def test_rich_unicode_tables_are_bundled(self) -> None:
        hidden = run_spec()["Analysis"].kwargs["hiddenimports"]
        tables = [name for name in hidden if name.startswith("rich._unicode_data.")]

        # rich builds this module name at runtime from the Unicode version, so
        # a frozen binary dies on the first wide glyph without them.
        assert len(tables) >= 20

    def test_every_bundled_table_exists_in_the_installed_rich(self) -> None:
        import importlib.util

        hidden = run_spec()["Analysis"].kwargs["hiddenimports"]
        tables = [name for name in hidden if name.startswith("rich._unicode_data.")]

        missing = [name for name in tables if importlib.util.find_spec(name) is None]
        assert not missing, f"listed but not present in rich: {missing}"

    def test_the_installed_rich_tables_are_all_listed(self) -> None:
        import rich._unicode_data as unicode_data

        available = {
            path.stem
            for path in Path(unicode_data.__file__).parent.glob("*.py")
            if path.stem != "__init__"
        }
        listed = {
            name.rsplit(".", 1)[-1]
            for name in run_spec()["Analysis"].kwargs["hiddenimports"]
            if name.startswith("rich._unicode_data.")
        }

        assert not available - listed, (
            f"rich ships tables the spec does not bundle: {available - listed}"
        )

    @pytest.mark.parametrize(
        "module",
        [
            "jaigent.llm.openai",
            "jaigent.llm.anthropic",
            "jaigent.llm.gemini",
            "jaigent.tools.files",
            "jaigent.tools.web",
            "jaigent.tools.shell",
        ],
    )
    def test_lazily_imported_modules_are_listed(self, module: str) -> None:
        assert module in run_spec()["Analysis"].kwargs["hiddenimports"]


class TestVersionConsistency:
    """pyproject.toml and jaigent.__version__ must agree.

    The release workflow refuses to publish when the tag disagrees with the
    source version, and it reads pyproject.toml. If the two drift, a release
    ships a binary reporting the wrong version.
    """

    def _pyproject_version(self) -> str:
        try:
            import tomllib
        except ModuleNotFoundError:  # Python 3.10
            tomllib = pytest.importorskip("tomli")

        with (ROOT / "pyproject.toml").open("rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])

    def test_they_match(self) -> None:
        from jaigent import __version__

        assert __version__ == self._pyproject_version()

    def test_it_looks_like_a_release_version(self) -> None:
        import re

        from jaigent import __version__

        assert re.fullmatch(r"\d+\.\d+\.\d+(?:[-.]?(?:a|b|rc|dev)\d*)?", __version__), __version__

    def test_the_changelog_mentions_it(self) -> None:
        from jaigent import __version__

        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        assert __version__ in changelog, f"CHANGELOG.md has no entry for {__version__}"
