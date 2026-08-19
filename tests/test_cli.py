"""CLI argument parsing and command dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeProvider
from jaigent import __version__, cli
from jaigent.errors import ConfigurationError, JaigentError
from jaigent.llm.base import AssistantMessage, ToolCall
from jaigent.session import Session, list_sessions


@pytest.fixture
def fake_agent(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Replace the provider factory so no network or key is needed."""
    script = [AssistantMessage(content="the answer")]

    def factory(settings):  # noqa: ANN001, ANN202
        return FakeProvider(list(script))

    monkeypatch.setattr("jaigent.agent.get_provider", factory)
    return script


@pytest.mark.usefixtures("clean_env")
class TestConfigCommand:
    def test_reports_missing_key_with_nonzero_exit(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        code = cli.main(["config"])
        out = capsys.readouterr().out

        assert code == 1
        assert "No API key configured" in out

    def test_shows_masked_key(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-supersecret")
        code = cli.main(["config"])
        out = capsys.readouterr().out

        assert code == 0
        assert "supersecret" not in out


@pytest.mark.usefixtures("clean_env")
class TestToolsCommand:
    def test_lists_default_tools(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["tools"]) == 0
        out = capsys.readouterr().out

        assert "web_search" in out
        assert "read_file" in out
        # The shell tool is not offered; only the hint about enabling it appears.
        assert "run_command is hidden" in out
        assert "Run a shell command" not in out

    def test_shell_tool_shown_when_allowed(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["tools", "--allow-shell"])
        out = capsys.readouterr().out

        assert "run_command" in out
        assert "Run a shell command" in out


@pytest.mark.usefixtures("clean_env", "fake_agent")
class TestRunCommand:
    def test_explicit_run(self, capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
        code = cli.main(["run", "what", "is", "up", "--workspace", str(tmp_path), "--no-color"])
        assert code == 0
        assert "the answer" in capsys.readouterr().out

    def test_bare_prompt_shorthand(self, capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
        code = cli.main(["hello there", "--workspace", str(tmp_path), "--no-color"])
        assert code == 0
        assert "the answer" in capsys.readouterr().out

    def test_no_args_prints_splash_with_logo(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main([]) == 0
        out = capsys.readouterr().out

        assert "jaigent" in out.lower() or "#" in out
        assert "searches the web" in out  # the tagline
        assert "jaigent chat" in out  # example commands
        assert "OPENAI_API_KEY" in out  # how to bring a key


@pytest.mark.usefixtures("clean_env")
class TestLogo:
    def test_logo_flag_prints_the_wordmark(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["--logo"]) == 0
        out = capsys.readouterr().out

        assert "searches the web" in out or "jaigent" in out.lower() or "██" in out
        assert __version__ in out

    def test_logo_respects_no_color(self, capsys: pytest.CaptureFixture) -> None:
        cli.main(["--logo", "--no-color"])
        assert "\x1b[" not in capsys.readouterr().out

    def test_logo_flag_skips_the_agent(self, capsys: pytest.CaptureFixture) -> None:
        # No API key is set, yet --logo must still succeed.
        assert cli.main(["--logo"]) == 0


@pytest.mark.usefixtures("clean_env")
class TestSettingsResolution:
    def test_flags_override_environment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("JAIGENT_MODEL", "from-env")
        args = cli.build_parser().parse_args(
            ["run", "x", "--model", "from-flag", "--workspace", str(tmp_path), "--max-steps", "3"]
        )
        settings = cli.resolve_settings(args)

        assert settings.model == "from-flag"
        assert settings.max_steps == 3
        assert settings.workspace == tmp_path.resolve()

    def test_environment_used_when_no_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JAIGENT_MODEL", "from-env")
        args = cli.build_parser().parse_args(["run", "x"])
        assert cli.resolve_settings(args).model == "from-env"

    def test_allow_shell_flag(self) -> None:
        args = cli.build_parser().parse_args(["run", "x", "--allow-shell"])
        assert cli.resolve_settings(args).allow_shell is True


@pytest.mark.usefixtures("clean_env")
class TestErrorHandling:
    def test_missing_api_key_exits_78(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        code = cli.main(["run", "hello"])
        assert code == 78
        assert "configuration error" in capsys.readouterr().err.lower()

    def test_empty_prompt(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        assert cli.main(["run", "   "]) == 2


def test_version_flag(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert "jaigent" in capsys.readouterr().out


@pytest.fixture
def session_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "sessions"
    monkeypatch.setenv("JAIGENT_SESSION_DIR", str(directory))
    return directory


@pytest.mark.usefixtures("clean_env")
class TestApprovalFlags:
    """--yes / --ask / --dry-run, and the tty-dependent default."""

    def _settings(self, argv: list[str], monkeypatch: pytest.MonkeyPatch, tty: bool = False):  # noqa: ANN202
        monkeypatch.setattr("sys.stdin.isatty", lambda: tty)
        monkeypatch.setattr("sys.stdout.isatty", lambda: tty)
        return cli.resolve_settings(cli.build_parser().parse_args(argv))

    def test_yes_means_auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._settings(["run", "x", "--yes"], monkeypatch).approval == "auto"

    def test_ask_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._settings(["run", "x", "--ask"], monkeypatch).approval == "ask"

    def test_dry_run_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._settings(["run", "x", "--dry-run"], monkeypatch).approval == "dry-run"

    def test_dry_run_beats_yes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = self._settings(["run", "x", "--yes", "--dry-run"], monkeypatch)
        assert settings.approval == "dry-run"

    def test_piped_output_defaults_to_auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Scripts must never hang waiting for a y/n that nobody can type.
        assert self._settings(["run", "x"], monkeypatch, tty=False).approval == "auto"

    def test_interactive_defaults_to_ask(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._settings(["run", "x"], monkeypatch, tty=True).approval == "ask"

    def test_env_var_is_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JAIGENT_APPROVAL", "dry-run")
        assert self._settings(["run", "x"], monkeypatch, tty=True).approval == "dry-run"


@pytest.mark.usefixtures("clean_env")
class TestStreamAndCostFlags:
    def test_streaming_on_by_default(self) -> None:
        args = cli.build_parser().parse_args(["run", "x"])
        assert cli.resolve_settings(args).stream is True

    def test_no_stream(self) -> None:
        args = cli.build_parser().parse_args(["run", "x", "--no-stream"])
        assert cli.resolve_settings(args).stream is False

    def test_cost_on_by_default(self) -> None:
        args = cli.build_parser().parse_args(["run", "x"])
        assert cli.resolve_settings(args).show_cost is True

    def test_no_cost(self) -> None:
        args = cli.build_parser().parse_args(["run", "x", "--no-cost"])
        assert cli.resolve_settings(args).show_cost is False


@pytest.mark.usefixtures("clean_env", "fake_agent")
class TestCostFooter:
    def test_footer_shows_tokens(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        def factory(settings):  # noqa: ANN001, ANN202
            return FakeProvider(
                [
                    AssistantMessage(
                        content="hi", usage={"prompt_tokens": 1000, "completion_tokens": 500}
                    )
                ]
            )

        monkeypatch.setattr("jaigent.agent.get_provider", factory)
        cli.main(["run", "x", "-w", str(tmp_path), "-m", "gpt-4o-mini", "--no-color"])
        out = capsys.readouterr().out

        assert "1,500 tokens" in out
        assert "$" in out

    def test_no_cost_hides_the_footer(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        def factory(settings):  # noqa: ANN001, ANN202
            return FakeProvider([AssistantMessage(content="hi", usage={"total_tokens": 99})])

        monkeypatch.setattr("jaigent.agent.get_provider", factory)
        cli.main(["run", "x", "-w", str(tmp_path), "--no-cost", "--no-color"])
        assert "tokens" not in capsys.readouterr().out


@pytest.mark.usefixtures("clean_env")
class TestSessionsCommand:
    def test_empty_store(self, session_dir: Path, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["sessions"]) == 0
        assert "No saved sessions" in capsys.readouterr().out

    def test_lists_saved_sessions(self, session_dir: Path, capsys: pytest.CaptureFixture) -> None:
        session = Session.new(provider="openai", model="gpt-4o-mini")
        session.title = "research pandas"
        session.messages = [{"role": "user", "content": "hi"}]
        session.save()

        assert cli.main(["sessions"]) == 0
        out = capsys.readouterr().out
        assert "research pandas" in out
        assert session.id in out

    def test_delete_one(self, session_dir: Path, capsys: pytest.CaptureFixture) -> None:
        session = Session.new()
        session.save()

        assert cli.main(["sessions", "--delete", session.id]) == 0
        assert list_sessions() == []

    def test_delete_all(self, session_dir: Path) -> None:
        for index in range(3):
            session = Session.new()
            session.id = f"2026010{index}-000000"
            session.save()

        cli.main(["sessions", "--delete", "all"])
        assert list_sessions() == []

    def test_delete_unknown(self, session_dir: Path, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["sessions", "--delete", "ghost"]) == 1


@pytest.mark.usefixtures("clean_env")
class TestChatResume:
    def test_unknown_session_is_an_error(
        self, session_dir: Path, capsys: pytest.CaptureFixture
    ) -> None:
        assert cli.main(["chat", "--resume", "nope"]) == 1
        assert "No session matching" in capsys.readouterr().err


@pytest.mark.usefixtures("clean_env")
def test_verbose_run_traces_tools(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    def factory(settings):  # noqa: ANN001, ANN202
        return FakeProvider(
            [
                AssistantMessage(tool_calls=[ToolCall("c1", "list_files", {})]),
                AssistantMessage(content="listed"),
            ]
        )

    monkeypatch.setattr("jaigent.agent.get_provider", factory)
    cli.main(["run", "list files", "--workspace", str(tmp_path), "--verbose", "--no-color"])
    captured = capsys.readouterr()

    assert "list_files" in captured.err
    assert "listed" in captured.out


class TestArgvNormalisation:
    """Shared options are accepted before the subcommand, where people type them."""

    def test_a_bare_prompt_becomes_run(self) -> None:
        assert cli.normalise_argv(["do the thing"]) == ["run", "do the thing"]

    def test_a_known_subcommand_is_untouched(self) -> None:
        assert cli.normalise_argv(["tools"]) == ["tools"]

    def test_a_slash_command_becomes_run(self) -> None:
        assert cli.normalise_argv(["/review", "x"]) == ["run", "/review", "x"]

    def test_empty_argv_is_untouched(self) -> None:
        assert cli.normalise_argv([]) == []

    def test_a_leading_option_moves_after_the_subcommand(self) -> None:
        """Regression: '/tmp' was read as the command name."""
        assert cli.normalise_argv(["--workspace", "/tmp", "tools"]) == [
            "tools",
            "--workspace",
            "/tmp",
        ]

    def test_a_short_option_moves_too(self) -> None:
        assert cli.normalise_argv(["-w", "/tmp", "config"]) == ["config", "-w", "/tmp"]

    def test_an_equals_form_needs_no_value_token(self) -> None:
        assert cli.normalise_argv(["--workspace=/tmp", "tools"]) == ["tools", "--workspace=/tmp"]

    def test_a_boolean_flag_moves_without_a_value(self) -> None:
        assert cli.normalise_argv(["-v", "tools"]) == ["tools", "-v"]

    def test_several_leading_options_all_move(self) -> None:
        assert cli.normalise_argv(["-v", "--workspace", "/tmp", "tools"]) == [
            "tools",
            "-v",
            "--workspace",
            "/tmp",
        ]

    def test_a_leading_option_with_a_bare_prompt_still_runs(self) -> None:
        assert cli.normalise_argv(["-v", "summarise this"]) == ["run", "-v", "summarise this"]

    @pytest.mark.parametrize("flag", ["--help", "-h", "--version", "--logo"])
    def test_top_level_only_options_are_left_alone(self, flag: str) -> None:
        assert cli.normalise_argv([flag]) == [flag]

    def test_options_with_no_command_are_left_alone(self) -> None:
        assert cli.normalise_argv(["--no-color"]) == ["--no-color"]


class TestWorkspaceValidation:
    """An unusable --workspace is caught up front, not at the first tool call."""

    def test_a_directory_is_accepted(self, tmp_path: Path) -> None:
        assert cli._resolve_workspace(str(tmp_path)) == tmp_path

    def test_none_stays_none(self) -> None:
        assert cli._resolve_workspace(None) is None

    def test_empty_stays_none(self) -> None:
        assert cli._resolve_workspace("") is None

    def test_a_missing_directory_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="does not exist"):
            cli._resolve_workspace(str(tmp_path / "nope"))

    def test_a_file_is_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("x")

        with pytest.raises(ConfigurationError, match="not a directory"):
            cli._resolve_workspace(str(target))

    def test_a_tilde_is_expanded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Path.expanduser reads HOME on POSIX but USERPROFILE on Windows, so a
        # test that sets only HOME silently checks the real home directory there.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        assert cli._resolve_workspace("~") == tmp_path


class TestRouteValidation:
    def test_an_empty_prompt_is_rejected(self, capsys: pytest.CaptureFixture) -> None:
        code = cli.main(["route", ""])

        assert code == 2
        assert "nothing to route" in capsys.readouterr().err.lower()

    def test_whitespace_only_is_rejected(self) -> None:
        assert cli.main(["route", "   "]) == 2

    def test_a_real_prompt_still_works(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["route", "refactor the parser"]) == 0
        assert "difficulty" in capsys.readouterr().out

    def test_free_flag_still_routes(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["route", "--free", "hi"]) == 0
        assert "difficulty" in capsys.readouterr().out


@pytest.mark.usefixtures("clean_env")
class TestProvidersCommand:
    def test_lists_key_urls(self, capsys: pytest.CaptureFixture) -> None:
        assert cli.main(["providers"]) == 0
        out = capsys.readouterr().out
        assert "openrouter" in out.lower()
        assert "OPENROUTER_API_KEY" in out
        assert "ollama" in out.lower()


class TestPluginsCommand:
    def test_empty_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "home"))
        monkeypatch.chdir(tmp_path)
        assert cli.main(["plugins", "list"]) == 0
        assert "No plugins yet" in capsys.readouterr().out

    def test_new_and_remove(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "home"))
        monkeypatch.chdir(tmp_path)
        assert cli.main(["plugins", "new", "hello"]) == 0
        assert (tmp_path / ".jaigent" / "plugins" / "hello.py").is_file()
        assert cli.main(["plugins", "remove", "hello"]) == 0
        assert not (tmp_path / ".jaigent" / "plugins" / "hello.py").exists()


@pytest.mark.usefixtures("clean_env")
class TestUpdateThreadIsAlwaysJoined:
    """Every exit path must join the background update-check thread.

    The thread is a daemon, so if ``main()`` returns without joining it the
    interpreter tears down while the worker may be mid-TLS-handshake. That
    crashed the process (SIGSEGV) on roughly a third of error-path runs.
    """

    @pytest.fixture
    def joins(self, monkeypatch: pytest.MonkeyPatch) -> list[object]:
        recorded: list[object] = []
        sentinel = object()

        monkeypatch.setattr("jaigent.updater.check_in_background", lambda: sentinel)
        monkeypatch.setattr(
            "jaigent.updater.finish_check", lambda thread, *a, **k: recorded.append(thread)
        )
        return recorded

    def test_joined_on_success(self, joins: list[object]) -> None:
        assert cli.main(["route", "refactor the parser"]) == 0
        assert len(joins) == 1

    def test_joined_on_configuration_error(
        self, joins: list[object], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert cli.main(["run", "hello"]) == 78
        assert len(joins) == 1, "configuration error returned without joining the update thread"

    def test_joined_on_jaigent_error(
        self, joins: list[object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(args):  # noqa: ANN001, ANN202
            raise JaigentError("boom")

        monkeypatch.setattr(cli, "cmd_tools", explode)
        assert cli.main(["tools"]) == 1
        assert len(joins) == 1, "JaigentError returned without joining the update thread"

    def test_joined_on_keyboard_interrupt(
        self, joins: list[object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def interrupt(args):  # noqa: ANN001, ANN202
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "cmd_tools", interrupt)
        assert cli.main(["tools"]) == 130
        assert len(joins) == 1, "KeyboardInterrupt returned without joining the update thread"

    def test_joined_even_when_a_handler_raises_something_unexpected(
        self, joins: list[object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(args):  # noqa: ANN001, ANN202
            raise RuntimeError("unhandled")

        monkeypatch.setattr(cli, "cmd_tools", explode)
        with pytest.raises(RuntimeError):
            cli.main(["tools"])
        assert len(joins) == 1, "an unexpected exception skipped the join"


class TestStreamPrinterRerender:
    """Streamed text is raw markup; it gets redrawn as markdown once complete."""

    def _console(self, **kwargs):  # noqa: ANN003, ANN202
        from io import StringIO

        from rich.console import Console

        defaults = {"width": 40, "file": StringIO(), "force_terminal": True}
        defaults.update(kwargs)
        return Console(**defaults)

    def _stream(self, console, text: str) -> str:  # noqa: ANN001
        printer = cli._StreamPrinter(console)
        printer(text)
        printer.finish()
        return console.file.getvalue()

    def test_raw_markup_is_shown_while_streaming(self) -> None:
        console = self._console()
        printer = cli._StreamPrinter(console)
        printer("**bold**")

        assert "**bold**" in console.file.getvalue()

    def test_the_asterisks_are_gone_once_finished(self) -> None:
        out = self._stream(self._console(), "**bold**")

        assert out.endswith("\n")
        assert "bold" in out
        # The rendered form replaces the raw one at the end of the output.
        assert "**bold**" not in out.split("\x1b[0J")[-1]

    def test_it_rewinds_over_exactly_what_it_wrote(self) -> None:
        out = self._stream(self._console(width=40), "hello")

        assert "\x1b[1A\x1b[0J" in out

    def test_a_wrapped_line_counts_every_row(self) -> None:
        # 85 characters at width 40 occupies three rows.
        out = self._stream(self._console(width=40), "x" * 85)

        assert "\x1b[3A\x1b[0J" in out

    def test_nothing_is_rewritten_when_piped(self) -> None:
        console = self._console(force_terminal=False)
        out = self._stream(console, "**bold**")

        assert "\x1b[" not in out
        assert "**bold**" in out

    def test_nothing_is_rewritten_without_colour(self) -> None:
        out = self._stream(self._console(no_color=True), "**bold**")

        assert "\x1b[0J" not in out

    def test_markdown_can_be_switched_off(self) -> None:
        console = self._console()
        printer = cli._StreamPrinter(console, markdown=False)
        printer("**bold**")
        printer.finish()

        assert "\x1b[0J" not in console.file.getvalue()

    def test_content_taller_than_the_window_is_left_alone(self) -> None:
        # It has already scrolled; cursor-up would clamp and erase the wrong rows.
        console = self._console(height=10)
        out = self._stream(console, "\n".join(f"line {i}" for i in range(40)))

        assert "\x1b[0J" not in out

    def test_an_empty_stream_writes_nothing(self) -> None:
        console = self._console()
        printer = cli._StreamPrinter(console)
        printer("")
        printer.finish()

        assert console.file.getvalue() == ""
        assert not printer.wrote

    def test_whitespace_only_output_is_not_rerendered(self) -> None:
        out = self._stream(self._console(), "   ")

        assert "\x1b[0J" not in out

    def test_the_streamed_text_is_kept(self) -> None:
        console = self._console()
        printer = cli._StreamPrinter(console)
        printer("one ")
        printer("two")

        assert printer.text == "one two"

    def test_a_code_fence_survives_streaming(self) -> None:
        out = self._stream(self._console(width=60), "```python\nx = 1\n```")

        assert "x = 1" in out
        assert "```" not in out.split("\x1b[0J")[-1]


class TestWrappedRows:
    @pytest.mark.parametrize(
        ("text", "width", "expected"),
        [
            ("", 40, 1),
            ("hello", 40, 1),
            ("hello\n", 40, 2),
            ("a\nb\nc", 40, 3),
            ("x" * 40, 40, 1),
            ("x" * 41, 40, 2),
            ("x" * 85, 40, 3),
        ],
    )
    def test_rows(self, text: str, width: int, expected: int) -> None:
        assert cli._wrapped_rows(text, width) == expected

    def test_a_zero_width_console_does_not_divide_by_zero(self) -> None:
        assert cli._wrapped_rows("hello", 0) >= 1


class TestCommandsTuple:
    """Every subparser from build_parser must have an entry in COMMANDS.

    normalise_argv uses the COMMANDS tuple to decide whether a leading token is
    a subcommand. A subparser without a corresponding entry is silently
    rewritten as a ``run`` prompt, which makes the subcommand unreachable.
    """

    def test_the_commands_tuple_covers_every_subparser(self) -> None:
        import argparse

        parser = cli.build_parser()
        # Collect subcommand names from argparse.
        subparsers_actions = [
            action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
        ]
        assert len(subparsers_actions) == 1, "expected exactly one subparsers group"
        expected = set(subparsers_actions[0].choices.keys())
        actual = set(cli.COMMANDS)
        missing = expected - actual
        extra = actual - expected

        assert not missing, (
            f"Subparser(s) not in COMMANDS tuple: {', '.join(sorted(missing))}. "
            "Add them so normalise_argv does not rewrite them as 'run'."
        )
        assert not extra, f"COMMANDS entry(ies) with no subparser: {', '.join(sorted(extra))}."
