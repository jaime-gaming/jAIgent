"""The GitHub Actions workflows.

These files can only really be exercised by pushing a tag, and the release one
runs on five platforms at once. That is a slow and public place to discover a
typo, so as much as possible is checked here: the YAML parses, the job graph is
sound, every shell block is valid shell, and the asset names the publish step
insists on are the ones the build matrix actually produces.

The workflows deliberately live one level up, in `.github/`, rather than in
`.github/workflows/`: the automation account that writes them cannot push to
that directory without the `workflows` token scope. `scripts/activate-ci.sh`
moves them into place. Both locations are accepted here.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent

#: Every platform a release must ship, and the asset each produces.
RELEASE_ASSETS = {
    "linux-x64": "jaigent-linux-x64",
    "linux-arm64": "jaigent-linux-arm64",
    "macos-x64": "jaigent-macos-x64",
    "macos-arm64": "jaigent-macos-arm64",
    "windows-x64": "jaigent-windows-x64",
}


def workflow_path(name: str) -> Path:
    """Find a workflow whether or not activate-ci.sh has been run."""
    for candidate in (
        ROOT / ".github" / "workflows" / f"{name}.yml",
        ROOT / ".github" / f"{name}.yml",
    ):
        if candidate.is_file():
            return candidate
    pytest.fail(f"{name}.yml is in neither .github/ nor .github/workflows/")


def load(name: str) -> dict[str, Any]:
    return yaml.safe_load(workflow_path(name).read_text(encoding="utf-8"))


def shell_steps(workflow: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Every ``run:`` block that is shell, as (job, step name, script)."""
    found = []
    for job_id, job in workflow["jobs"].items():
        for index, step in enumerate(job.get("steps", [])):
            script = step.get("run")
            if script and step.get("shell", "bash") != "pwsh":
                found.append((job_id, step.get("name", f"step {index}"), script))
    return found


def usable_bash() -> str | None:
    """Path to a bash that can actually run a script, or None.

    On Windows runners `bash` on PATH is often the WSL launcher stub, which has
    no distribution installed and answers every invocation with a UTF-16 notice
    and exit code 1. Finding the executable is therefore not enough; it has to
    be asked to do something trivial first.
    """
    found = shutil.which("bash")
    if not found:
        return None
    try:
        probe = subprocess.run(  # noqa: S603
            [found, "-c", "exit 0"], capture_output=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
        return None
    return found if probe.returncode == 0 else None


class TestBothWorkflowsAreWellFormed:
    @pytest.mark.parametrize("name", ["ci", "release"])
    def test_it_parses(self, name: str) -> None:
        assert isinstance(load(name)["jobs"], dict)

    @pytest.mark.parametrize("name", ["ci", "release"])
    def test_every_job_has_steps(self, name: str) -> None:
        for job_id, job in load(name)["jobs"].items():
            assert job.get("steps"), f"{job_id} has no steps"

    @pytest.mark.parametrize("name", ["ci", "release"])
    def test_needs_only_reference_real_jobs(self, name: str) -> None:
        jobs = load(name)["jobs"]
        for job_id, job in jobs.items():
            needs = job.get("needs", [])
            needs = [needs] if isinstance(needs, str) else needs
            for dependency in needs:
                assert dependency in jobs, f"{job_id} needs unknown job {dependency!r}"

    @pytest.mark.parametrize("name", ["ci", "release"])
    def test_the_job_graph_is_acyclic(self, name: str) -> None:
        jobs = load(name)["jobs"]

        def needs_of(job_id: str) -> list[str]:
            needs = jobs[job_id].get("needs", [])
            return [needs] if isinstance(needs, str) else list(needs)

        def walk(job_id: str, seen: tuple[str, ...]) -> None:
            assert job_id not in seen, f"cycle: {' -> '.join([*seen, job_id])}"
            for dependency in needs_of(job_id):
                walk(dependency, (*seen, job_id))

        for job_id in jobs:
            walk(job_id, ())

    @pytest.mark.parametrize("name", ["ci", "release"])
    def test_every_action_is_pinned_to_a_major(self, name: str) -> None:
        text = workflow_path(name).read_text(encoding="utf-8")
        for use in re.findall(r"uses:\s*(\S+)", text):
            assert "@" in use, f"unpinned action: {use}"

    @pytest.mark.parametrize("name", ["ci", "release"])
    def test_every_shell_block_is_valid_shell(self, name: str) -> None:
        bash = usable_bash()
        if bash is None:
            pytest.skip("no working bash on this platform")

        for job_id, step_name, script in shell_steps(load(name)):
            # ${{ ... }} is substituted by Actions before the shell sees it.
            source = re.sub(r"\$\{\{[^}]*\}\}", "EXPR", script)
            # newline="\n" matters: the default on Windows writes CRLF, and a
            # heredoc terminator followed by \r never matches its opener, so
            # every script containing one would look like a syntax error.
            with tempfile.NamedTemporaryFile(
                "w", suffix=".sh", delete=False, newline="\n", encoding="utf-8"
            ) as handle:
                handle.write(source)
                path = Path(handle.name)
            try:
                result = subprocess.run(  # noqa: S603
                    [bash, "-n", str(path)], capture_output=True, text=True
                )
            finally:
                path.unlink()

            assert result.returncode == 0, f"{name}.yml {job_id} / {step_name}:\n{result.stderr}"


class TestReleaseWorkflow:
    def test_it_triggers_on_version_tags(self) -> None:
        # yaml parses a bare `on:` key as the boolean True.
        triggers = load("release")[True]

        assert triggers["push"]["tags"] == ["v*"]
        assert "workflow_dispatch" in triggers

    def test_it_can_write_releases(self) -> None:
        assert load("release")["permissions"]["contents"] == "write"

    def test_the_matrix_covers_every_target(self) -> None:
        matrix = load("release")["jobs"]["build"]["strategy"]["matrix"]["include"]

        assert {entry["label"] for entry in matrix} == set(RELEASE_ASSETS)

    def test_each_target_builds_the_expected_asset(self) -> None:
        matrix = load("release")["jobs"]["build"]["strategy"]["matrix"]["include"]

        assert {entry["label"]: entry["asset"] for entry in matrix} == RELEASE_ASSETS

    def test_windows_ships_a_zip_and_the_rest_tarballs(self) -> None:
        matrix = load("release")["jobs"]["build"]["strategy"]["matrix"]["include"]

        for entry in matrix:
            expected = "zip" if entry["label"].startswith("windows") else "tar.gz"
            assert entry["archive"] == expected, entry

    def test_the_runners_are_pinned(self) -> None:
        # "-latest" drifts. macos-latest moving to ARM would silently drop the
        # Intel build, and a newer Linux image raises the glibc floor.
        matrix = load("release")["jobs"]["build"]["strategy"]["matrix"]["include"]

        for entry in matrix:
            if entry["label"] == "windows-x64":
                continue  # only one Windows image is offered
            assert not entry["os"].endswith("-latest"), entry["os"]

    def test_the_version_is_checked_before_anything_is_built(self) -> None:
        jobs = load("release")["jobs"]

        assert "verify" in jobs
        for downstream in ("build", "wheel"):
            needs = jobs[downstream].get("needs", [])
            needs = [needs] if isinstance(needs, str) else needs
            assert "verify" in needs, f"{downstream} would run before the tag is validated"

    def test_publish_waits_for_every_build(self) -> None:
        needs = load("release")["jobs"]["publish"]["needs"]

        assert {"build", "wheel"} <= set(needs)

    def test_publish_insists_on_the_assets_the_matrix_produces(self) -> None:
        release = load("release")
        matrix = release["jobs"]["build"]["strategy"]["matrix"]["include"]
        guard = "\n".join(
            step["run"]
            for step in release["jobs"]["publish"]["steps"]
            if "run" in step and "partial" in step.get("name", "").lower()
        )

        assert guard, "there is no step refusing to publish a partial release"
        for entry in matrix:
            filename = f"{entry['asset']}.{entry['archive']}"
            assert filename in guard, f"publish does not require {filename}"

    def test_the_smoke_test_renders_the_logo(self) -> None:
        # --logo draws wide glyphs, which is how a frozen binary missing rich's
        # runtime-selected unicode tables gives itself away.
        scripts = "\n".join(script for _, _, script in shell_steps(load("release")))

        assert "--logo" in scripts

    def test_archives_are_re_extracted_before_publishing(self) -> None:
        names = [step.get("name", "") for step in load("release")["jobs"]["build"]["steps"]]

        assert any("re-extract" in name.lower() for name in names)

    def test_the_wheel_is_installed_and_run(self) -> None:
        scripts = "\n".join(
            step["run"] for step in load("release")["jobs"]["wheel"]["steps"] if "run" in step
        )

        assert "pip install" in scripts
        assert "--version" in scripts


class TestContinuousIntegration:
    def test_it_runs_on_pull_requests(self) -> None:
        assert "pull_request" in load("ci")[True]

    def test_it_tests_every_supported_python(self) -> None:
        matrix = load("ci")["jobs"]["test"]["strategy"]["matrix"]
        classifiers = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        supported = set(re.findall(r"Programming Language :: Python :: (3\.\d+)", classifiers))

        assert supported <= set(matrix["python-version"]), (
            "a Python version is advertised in the classifiers but never tested"
        )
