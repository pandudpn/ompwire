"""The CLI help is the first thing a new machine shows; it has to be usable."""

from __future__ import annotations

import pytest

from ompwire import __main__ as cli


def test_no_arguments_prints_help_instead_of_opening_a_chat(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "usage: ompwire" in out
    assert "ompwire setup" in out and "ompwire serve" in out
    assert "Run `ompwire chat`" in out


def test_help_command_lists_commands(capsys):
    assert cli.main(["help"]) == 0
    out = capsys.readouterr().out
    for command in ("setup", "serve", "doctor", "update", "uninstall", "chat"):
        assert command in out


def test_help_for_one_command_shows_its_examples(capsys):
    assert cli.main(["help", "setup"]) == 0
    out = capsys.readouterr().out
    assert "usage: ompwire setup" in out
    assert "ompwire setup --yes" in out


def test_help_for_an_unknown_command_fails_loudly(capsys):
    assert cli.main(["help", "nonsense"]) == 2
    captured = capsys.readouterr()
    assert "unknown command: nonsense" in captured.err
    assert "usage: ompwire" in captured.out


def test_every_command_has_a_description_and_examples(capsys):
    parser = cli.build_parser()
    subparsers = next(action for action in parser._actions
                      if action.__class__.__name__ == "_SubParsersAction")
    for name, sub in subparsers.choices.items():
        assert sub.description, f"{name} has no description"
        assert "examples:" in (sub.epilog or ""), f"{name} has no examples"


def test_unknown_flag_exits_two(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["serve", "--nope"])
    assert excinfo.value.code == 2
