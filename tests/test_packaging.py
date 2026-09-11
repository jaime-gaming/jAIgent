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


def _stub_pyinstaller() -> None:
    """The spec imports ``collect_submodules``; CI does not install PyInstaller."""
    import types

    if "PyInstaller.utils.hooks" in sys.modules:
        return

    def collect_submodules(package: str, filter=None) -> list[str]:  # noqa: A002
        import importlib
        import pkgutil

        found = [package]
        try:
            module = importlib.import_module(package)
        except Exception:  # pragma: no cover - missing optional package
            return found
        paths = getattr(module, "__path__", None)
        if not paths:
            return found
        for item in pkgutil.walk_packages(paths, prefix=package + "."):
            if filter is None or filter(item.name):
                found.append(item.name)
        return found

    pyi = types.ModuleType("PyInstaller")
    utils = types.ModuleType("PyInstaller.utils")
    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_submodules = collect_submodules  # type: ignore[attr-defined]
    sys.modules.setdefault("PyInstaller", pyi)
    sys.modules.setdefault("PyInstaller.utils", utils)
    sys.modules["PyInstaller.utils.hooks"] = hooks


def run_spec(*, platform: str = "linux", spec_path: Path | None = None) -> dict[str, Recorder]:
    """Execute the spec on a pretend platform and return what it built."""
    _stub_pyinstaller()
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
            "jaigent.mcp",
            "jaigent.plugins",
            "jaigent.memory",
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


class TestWindowsVersionResource:
    """The exe's version resource must carry the four-part version.

    The release workflow's Windows check compares
    ``(Get-Item).VersionInfo.FileVersion`` with ``<version>.0`` and refuses to
    package the binary otherwise. The spec used to write the bare
    "0.5.6" (three parts) into the resource and a hardcoded (0, 5, 5, 0)
    fallback tuple, so the check could never pass and the Windows build —
    and with it the whole release — failed after a successful compile.
    """

    def test_the_version_file_carries_the_four_part_version(self) -> None:
        from jaigent import __version__

        built = run_spec(platform="win32")
        version_file = built["EXE"].kwargs["version"]
        assert version_file is not None
        text = Path(version_file).read_text(encoding="utf-8")

        expected = f"{__version__}.0"
        assert f"u'{expected}'" in text, f"expected {expected!r} in the version resource"
        assert "0, 5, 5" not in text, "a stale hardcoded version tuple is in the spec"

    def test_the_filevers_tuple_matches_the_version(self) -> None:
        from jaigent import __version__

        parts = [int(p) for p in __version__.split(".")]
        while len(parts) < 4:
            parts.append(0)
        expected = f"filevers={tuple(parts)}"

        built = run_spec(platform="win32")
        text = Path(built["EXE"].kwargs["version"]).read_text(encoding="utf-8")
        assert expected in text, f"expected {expected!r} in the version resource"

    def test_the_manifest_version_is_derived(self) -> None:
        from jaigent import __version__

        manifest = run_spec(platform="win32")["EXE"].kwargs["manifest"]
        assert f'version="{__version__}.0"' in manifest

    def test_the_workflow_expects_the_same_version(self) -> None:
        import re

        from jaigent import __version__

        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        assert re.search(rf'-ne ["\']{re.escape(__version__)}\.0["\']', workflow), (
            "the workflow's AV-safe check expects a different version than the source"
        )


class TestInstallerScripts:
    """The installers are piped straight into a user's shell. Lint them here.

    CI runs shellcheck too, but only on Linux and only after a push. Running it
    in the suite means a mistake in the one script users execute blind is caught
    before it leaves the machine.
    """

    SCRIPTS = ["packaging/install.sh", "scripts/activate-ci.sh"]

    def _shellcheck(self) -> str:
        import shutil

        # shellcheck-py puts the binary beside the running interpreter.
        found = shutil.which("shellcheck") or shutil.which(
            "shellcheck", path=str(Path(sys.executable).parent)
        )
        if not found:
            pytest.skip("shellcheck is not installed")
        return found

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_it_exists_and_is_executable_shell(self, script: str) -> None:
        path = ROOT / script

        assert path.is_file(), script
        assert path.read_text(encoding="utf-8").startswith("#!"), "no shebang"

    def test_install_sh_passes_shellcheck_as_posix_sh(self) -> None:
        import subprocess

        result = subprocess.run(  # noqa: S603
            [self._shellcheck(), "-s", "sh", str(ROOT / "packaging" / "install.sh")],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stdout or result.stderr

    def test_activate_ci_passes_shellcheck(self) -> None:
        import subprocess

        result = subprocess.run(  # noqa: S603
            [self._shellcheck(), str(ROOT / "scripts" / "activate-ci.sh")],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stdout or result.stderr

    def test_install_sh_is_posix_and_not_bash(self) -> None:
        # It is fetched and run with `sh`, which is dash on Debian and Ubuntu.
        shebang = (ROOT / "packaging" / "install.sh").read_text(encoding="utf-8").splitlines()[0]

        assert shebang.rstrip().endswith("sh"), shebang
        assert "bash" not in shebang, "install.sh must not require bash"


class TestInstallersRefuseAnUnverifiedBinary:
    """Both installers used to shrug and install anyway.

    A checksum that cannot be checked is not a checksum: on a machine with no
    digest tool, or whose TLS is intercepted so the checksums cannot be
    fetched, the old code installed whatever arrived and called it verified.
    """

    def test_install_sh_fails_without_a_digest_tool(self) -> None:
        script = (ROOT / "packaging" / "install.sh").read_text(encoding="utf-8")

        assert 'actual=""' not in script, "it still falls through to installing unverified"
        assert "no sha256sum or shasum available" in script

    def test_install_sh_never_silently_skips_the_check(self) -> None:
        script = (ROOT / "packaging" / "install.sh").read_text(encoding="utf-8")

        assert "skipping verification" not in script

    def test_install_ps1_fails_when_the_checksums_cannot_be_fetched(self) -> None:
        script = (ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")

        assert "skipping verification" not in script
        assert "cannot be verified" in script

    def test_install_ps1_fails_on_a_checksum_with_no_entry_for_the_asset(self) -> None:
        script = (ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")

        assert "has no entry for" in script


class TestInstallersCanReplaceARunningBinary:
    """`jaigent update` replaces the binary it is running from.

    On POSIX that already worked — same-filesystem `mv` is `rename()`, which
    swaps the directory entry and never opens the executing inode. Windows is
    the case that fails: a running executable cannot be opened for writing, so
    `Copy-Item -Force` over `jaigent.exe` errors out at exactly the moment the
    user asked for an update. Renaming it aside first is allowed.
    """

    def test_install_sh_unlinks_before_renaming(self) -> None:
        """So a cross-filesystem $BIN_DIR does not degrade to truncate-and-copy."""
        script = (ROOT / "packaging" / "install.sh").read_text(encoding="utf-8")
        unlink = script.index('rm -f "$BIN_DIR/jaigent"')
        rename = script.index('mv "$tmp/jaigent" "$BIN_DIR/jaigent"')

        assert unlink < rename

    def test_install_sh_reports_a_failed_install(self) -> None:
        script = (ROOT / "packaging" / "install.sh").read_text(encoding="utf-8")

        assert 'die "could not install to' in script

    def test_install_ps1_moves_an_in_use_executable_aside(self) -> None:
        script = (ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")

        assert "Move-Item" in script
        assert "another process" in script
