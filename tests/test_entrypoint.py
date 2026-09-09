"""The `python -m jaigent` entry point."""

from __future__ import annotations

import runpy
import sys

import pytest


def test_python_m_jaigent_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    import jaigent

    monkeypatch.setattr(sys, "argv", ["jaigent", "--version"])
    with pytest.raises(SystemExit) as exit:
        runpy.run_module("jaigent", run_name="__main__")

    assert exit.value.code == 0
    assert jaigent.__version__ in capsys.readouterr().out
