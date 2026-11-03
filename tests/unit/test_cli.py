"""The CLI must start in CI: a broken import while building the parser breaks every `jr` command (and every DAG)."""

from jobradar.cli import main


def test_cli_parser_builds_and_lists_sources(capsys):
    assert main(["sources"]) == 0
    assert capsys.readouterr().out
