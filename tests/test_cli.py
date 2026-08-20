"""dry-run smoke test — the default local loop must stay green (SPEC §10)."""

from radar.cli import main


def test_dry_run_prints_parsed_postings(capsys):
    assert main(["dry-run"]) == 0
    out = capsys.readouterr().out
    # One adapter (greenhouse) spanning two fixture boards; HRT's 3 intern
    # postings survive the prefilter, Anthropic's 3 non-intern rows drop.
    assert "dry-run: 1 source(s) ok, 0 failure(s), 3 new posting(s)" in out
    # HRT's Summer 2027 internships survive the prefilter into the would-be digest.
    assert "Hudson River Trading" in out
    assert "Summer 2027" in out


def test_unimplemented_commands_exit_2(capsys):
    assert main(["digest"]) == 2
    assert "M2" in capsys.readouterr().err
