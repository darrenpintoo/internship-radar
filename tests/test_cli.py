"""dry-run smoke test — the default local loop must stay green (SPEC §10)."""

from radar.cli import main


def test_dry_run_prints_parsed_postings(capsys):
    assert main(["dry-run"]) == 0
    out = capsys.readouterr().out
    # All seven adapters replay their recorded fixtures with zero failures.
    assert "dry-run: 7 source(s) ok, 0 failure(s)," in out
    # A representative posting from each adapter family made it through.
    assert "Hudson River Trading" in out  # greenhouse
    assert "Palantir" in out  # lever
    assert "Modal" in out  # ashby
    assert "SIG — Quantitative Research Internship: Summer 2027" in out  # icims
    assert "github_trackers" in out  # tracker modes
    # HRT's Summer 2027 internships survive the prefilter into the would-be digest.
    assert "Hudson River Trading" in out
    assert "Summer 2027" in out


def test_unimplemented_commands_exit_2(capsys):
    assert main(["backfill"]) == 2
    assert "M4" in capsys.readouterr().err
