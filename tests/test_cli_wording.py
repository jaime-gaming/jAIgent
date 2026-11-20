"""What the CLI says when something is not right.

These are the messages a person meets on the worst day they will have with
this program: a missing key, a flag that does not exist, a command they
half-remember. Each one has to name the problem and the way out, in words a
non-system-administrator already knows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jaigent import cli
from jaigent.errors import ConfigurationError


def run_cli(argv: list[str]) -> int:
    """Call ``main`` and turn argparse's ``SystemExit`` into the exit code."""
    with pytest.raises(SystemExit) as raised:
        cli.main(argv)
    code = raised.value.code
    return code if isinstance(code, int) else 0


class TestArgumentErrors:
    """argparse's default output is a compiler error with an RSVP."""

    def test_an_unknown_option_is_one_plain_sentence(self, capsys: pytest.CaptureFixture) -> None:
        code = run_cli(["--nonsense"])

        assert code == 2
        err = capsys.readouterr().err
        assert "I don't know the option --nonsense." in err
        assert "jaigent --help" in err
        # No usage banner: six lines of flags above the one useful sentence.
        assert "usage:" not in err

    def test_a_missing_argument_names_the_command(self, capsys: pytest.CaptureFixture) -> None:
        run_cli(["rewind"])

        err = capsys.readouterr().err
        assert "`jaigent rewind` is missing id." in err
        assert "`jaigent rewind --help`" in err

    def test_a_bad_subcommand_choice_lists_what_it_does_take(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        run_cli(["settings", "get", "model"])

        err = capsys.readouterr().err
        assert "does not take 'get'" in err
        # Rich folds the line, so the options are checked one by one.
        for option in ("'list'", "'set'", "'unset'", "'path'"):
            assert option in err

    def test_no_animation_is_accepted_after_a_subcommand(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # It used to be a top-level-only flag, so `jaigent doctor
        # --no-animation` failed with "unrecognized arguments".
        monkeypatch.setenv("HOME", str(tmp_path))
        args = cli.build_parser().parse_args(["doctor", "--no-animation"])

        assert args.no_animation is True


class TestMissingKey:
    """The first thing a new user runs into, so it has to be short."""

    def test_it_names_the_command_that_asks(self) -> None:
        from jaigent.config import Settings

        with pytest.raises(ConfigurationError) as raised:
            Settings(api_key=None).require_api_key()

        message = str(raised.value)
        assert "jaigent auth set openai" in message
        # chat asks, so nobody has to learn two commands on day one.
        assert "jaigent chat" in message
        assert "never ships with a key" not in message, "the aside was noise"

    def test_it_keeps_its_line_breaks(self, capsys: pytest.CaptureFixture) -> None:
        from jaigent.config import Settings

        try:
            Settings(api_key=None).require_api_key()
        except ConfigurationError as exc:
            cli.err_console.print(cli.clean_block(exc))

        printed = capsys.readouterr().err
        # A flattened block turned the three-step fix into one long sentence.
        assert "\n" in printed.strip()
        assert "Then run:" in printed

    def test_auth_set_rejects_an_unknown_provider_before_prompting(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        # Asking for a key first and rejecting the name afterwards wasted a
        # copy-paste and left the key typed into nothing.
        code = cli.cmd_auth(cli.build_parser().parse_args(["auth", "set", "nope"]))

        assert code == 2
        out = capsys.readouterr().err
        assert "There is no nope provider." in out
        assert "openai" in out


class TestFriendlyErrors:
    def test_the_403_advice_has_no_dash_and_no_jargon(self) -> None:
        from jaigent.config import Settings

        headline, advice = cli.friendly_error(
            Exception("HTTP 403 forbidden"), Settings(api_key="k", provider="openai")
        )

        assert chr(0x2014) not in advice
        # A 403 is not a rejected key, so the advice must not say it is.
        assert "not allowed" in advice
        assert "auth set" not in advice
        assert headline

    def test_a_dns_failure_says_what_failed(self) -> None:
        headline, _ = cli.friendly_error(Exception("getaddrinfo failed"), None)

        assert headline.startswith("Could not reach the provider")
        assert chr(0x2014) not in headline
