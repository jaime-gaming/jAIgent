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
        # Read through the folding: a console may wrap the list between a
        # quote and the word it quotes, which is what CI did on one runner.
        flat = " ".join(err.replace("'", " ").split())
        for option in ("list", "set", "unset", "path"):
            assert option in flat

    def test_no_animation_is_accepted_after_a_subcommand(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # It used to be a top-level-only flag, so `jaigent doctor
        # --no-animation` failed with "unrecognized arguments".
        monkeypatch.setenv("HOME", str(tmp_path))
        args = cli.build_parser().parse_args(["doctor", "--no-animation"])

        assert args.no_animation is True


class TestAFlagGivenToAGroupSurvivesItsAction:
    """The shared flags belong to the group, and to nothing below it.

    A nested parser writes its own defaults over the namespace it is handed. So
    while `schedule`, `keys` and `commands` carried the shared flags on the
    group *and* on every action, `jaigent schedule --workspace DIR list` parsed
    without complaint and then ignored the flag it had just been given: the
    action's own default landed on top of the value the group had stored.
    Nothing said so, and the command went on working in the wrong directory.
    """

    @pytest.mark.parametrize(
        ("argv", "flag", "expected"),
        [
            (["schedule", "--workspace", "DIR", "list"], "workspace", "DIR"),
            (["keys", "--workspace", "DIR", "list"], "workspace", "DIR"),
            (["commands", "--workspace", "DIR", "list"], "workspace", "DIR"),
            (["skills", "--workspace", "DIR", "list"], "workspace", "DIR"),
            (["plugins", "--workspace", "DIR", "list"], "workspace", "DIR"),
            (["settings", "--workspace", "DIR", "list"], "workspace", "DIR"),
            (["auth", "--no-color", "list"], "no_color", True),
            (["beta", "--workspace", "DIR", "join"], "workspace", "DIR"),
            (["schedule", "--no-color", "add", "x", "--every", "2h"], "no_color", True),
            (["keys", "--no-color", "new", "lab"], "no_color", True),
        ],
    )
    def test_the_group_keeps_what_it_was_given(
        self, argv: list[str], flag: str, expected: object
    ) -> None:
        args = cli.build_parser().parse_args(argv)

        assert getattr(args, flag) == expected

    @pytest.mark.parametrize(
        ("argv", "flag", "expected"),
        [
            (["skills", "new", "deploy", "-d", "d", "--workspace", "DIR"], "workspace", "DIR"),
            (["commands", "new", "review", "-d", "d", "--workspace", "DIR"], "workspace", "DIR"),
            (["schedule", "add", "x", "--every", "1h", "-w", "DIR"], "workspace", "DIR"),
            (["keys", "list", "--no-color"], "no_color", True),
            (["settings", "list", "--no-color"], "no_color", True),
            (["auth", "list", "--no-color"], "no_color", True),
            (["plugins", "list", "--no-color"], "no_color", True),
            (["beta", "join", "--no-color"], "no_color", True),
        ],
    )
    def test_the_same_flag_works_after_the_action_too(
        self, argv: list[str], flag: str, expected: object
    ) -> None:
        """Trailing flags are where everybody puts them, so they have to count.

        `jaigent schedule add "check the news" --every 2h -w DIR` reads naturally
        and used to work; a group that carried the flags on both levels lost the
        value instead, and a group that carried them only on top refused it. The
        actions inherit a copy whose defaults are suppressed, which gives both
        spellings one meaning and leaves an action's help to its own options.
        """
        args = cli.build_parser().parse_args(argv)

        assert getattr(args, flag) == expected

    #: Every action of every noun command, with the arguments it needs. The
    #: shared flags have to work on either side of each of these, which is 128
    #: combinations and the reason the flags are defined once and inherited.
    ACTIONS: tuple[list[str], ...] = (
        ["auth", "list"],
        ["auth", "set", "openai"],
        ["auth", "unset", "openai"],
        ["beta", "join"],
        ["beta", "leave"],
        ["beta", "status"],
        ["commands", "list"],
        ["commands", "new", "review"],
        ["commands", "remove", "review"],
        ["commands", "show", "review"],
        ["keys", "list"],
        ["keys", "new", "lab"],
        ["keys", "revoke", "lab"],
        ["plugins", "list"],
        ["plugins", "new", "hello"],
        ["plugins", "remove", "hello"],
        ["schedule", "add", "check the news", "--every", "1h"],
        ["schedule", "list"],
        ["schedule", "pause", "task-1"],
        ["schedule", "remove", "task-1"],
        ["schedule", "resume", "task-1"],
        ["schedule", "run"],
        ["schedule", "show", "task-1"],
        ["settings", "list"],
        ["settings", "path"],
        ["settings", "set", "model", "gpt-4o"],
        ["settings", "unset", "model"],
        ["skills", "list"],
        ["skills", "new", "changelog"],
        ["skills", "remove", "changelog"],
        ["skills", "show", "changelog"],
        ["web", "login"],
    )

    @pytest.mark.parametrize("argv", ACTIONS)
    def test_every_action_takes_the_shared_flags_on_either_side(self, argv: list[str]) -> None:
        parser = cli.build_parser()

        for candidate in ([argv[0], "--no-color", *argv[1:]], [*argv, "--no-color"]):
            assert parser.parse_args(candidate).no_color is True, candidate

        for candidate in (
            [argv[0], "--workspace", "DIR", *argv[1:]],
            [*argv, "--workspace", "DIR"],
        ):
            assert parser.parse_args(candidate).workspace == "DIR", candidate

    @pytest.mark.parametrize("argv", ACTIONS)
    def test_every_action_can_explain_itself(self, argv: list[str]) -> None:
        """A help screen that errors is worse than no help screen."""
        with pytest.raises(SystemExit) as raised:
            cli.build_parser().parse_args([*argv, "--help"])

        assert raised.value.code == 0

    def test_sessions_help_shows_the_actions_it_now_accepts(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """The flags were all this help showed, so the words looked unsupported.

        `sessions --help` is the one place that could have said `sessions show
        <id>` works, and it did not, which is how a spelling that works comes to
        look like a spelling that does not.
        """
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["sessions", "--help"])

        text = capsys.readouterr().out
        assert "jaigent sessions show <id>" in text
        assert "jaigent sessions delete <id>" in text
        assert "--show" in text  # the flag spelling is documented beside it

    def test_an_action_still_explains_itself_and_not_the_whole_cli(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """The shared flags are accepted after the action without being listed.

        `commands new --help` used to print twenty options a command file has no
        use for, and the four that decided anything were lost among them.
        """
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["commands", "new", "--help"])

        text = capsys.readouterr().out
        assert "--template" in text
        assert "--user" in text
        assert "--provider" not in text
        assert "--temperature" not in text
        assert "--workspace" not in text


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
