"""The package's front door: lazy imports, and the aliases people actually type."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

import jaigent
from jaigent import cli


class TestLazyExports:
    def test_every_public_name_resolves(self) -> None:
        for name in jaigent.__all__:
            assert getattr(jaigent, name) is not None

    def test_the_exports_are_the_real_objects(self) -> None:
        """The lazy table must point at the same classes, not copies."""
        from jaigent.agent import Agent

        assert jaigent.Agent is Agent
        assert jaigent.Agent is jaigent.Agent  # cached after the first access

    def test_an_unknown_name_raises_attribute_error(self) -> None:
        with pytest.raises(AttributeError):
            jaigent.does_not_exist  # noqa: B018

    def test_importing_the_package_does_not_drag_in_the_agent(self) -> None:
        """``jaigent --version`` must not pay for httpx and the tool registry."""
        code = "import sys, jaigent; print('jaigent.agent' in sys.modules)"
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )

        assert out.stdout.strip() == "False"

    def test_importing_the_package_is_quick(self) -> None:
        code = "import time; s=time.perf_counter(); import jaigent; print(time.perf_counter()-s)"
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )

        # Importing the agent used to cost ~90 ms; the package alone is ~7 ms.
        # The bound is deliberately loose: this catches a re-introduced eager
        # import, not a slow machine.
        assert float(out.stdout) < 0.06


class TestAliases:
    def test_help_is_not_sent_to_the_model(self) -> None:
        """It used to be a prompt: four seconds, then a network error."""
        assert cli.normalise_argv(["help"]) == ["--help"]

    def test_version_is_an_alias(self) -> None:
        assert cli.normalise_argv(["version"]) == ["--version"]

    def test_upgrade_is_update(self) -> None:
        assert cli.normalise_argv(["upgrade"]) == ["update"]

    def test_extra_words_are_kept_for_the_real_command(self) -> None:
        assert cli.normalise_argv(["upgrade", "--check"]) == ["update", "--check"]

    def test_a_real_prompt_is_still_a_prompt(self) -> None:
        assert cli.normalise_argv(["explain help output"]) == ["run", "explain help output"]

    @pytest.mark.parametrize("word", ["help", "version", "upgrade"])
    def test_an_alias_never_reaches_the_provider(self, word: str) -> None:
        assert cli.normalise_argv([word])[0].startswith("-") or (
            cli.normalise_argv([word])[0] in cli.COMMANDS
        )

    def test_it_is_fast_compared_with_a_run(self) -> None:
        """A help screen is local; it must never wait on a provider.

        ``--help`` finishes through argparse's ``SystemExit(0)``, which is the
        normal way a CLI help screen exits.
        """
        started = time.perf_counter()
        with pytest.raises(SystemExit) as exit_info:
            cli.main(["help"])
        elapsed = time.perf_counter() - started

        assert exit_info.value.code == 0
        assert elapsed < 1.0
