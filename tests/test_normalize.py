"""Normalize + prefilter tests (SPEC §6.1, §6.2, §10)."""

from radar import normalize


class TestTitleNormHash:
    def test_four_season_variants_share_one_hash(self):
        # SPEC §6.1 test vectors — all four decorations must collide.
        variants = [
            "Software Engineering Intern - Summer 2027 (Remote)",
            "Software Engineering Intern — Summer 2027",
            "2027 Summer Software Engineering Intern",
            "Software Engineering Intern (Summer 2027)",
        ]
        hashes = {normalize.content_hash("acme", normalize.title_norm(t)) for t in variants}
        assert len(hashes) == 1

    def test_season_word_survives_year_removal(self):
        # Summer vs Fall co-ops must NOT merge.
        summer = normalize.title_norm("Software Intern (Summer 2027)")
        fall = normalize.title_norm("Software Intern (Fall 2027)")
        assert "summer" in summer and "fall" in fall
        assert normalize.content_hash("acme", summer) != normalize.content_hash("acme", fall)

    def test_req_ids_dropped(self):
        assert normalize.title_norm("SWE Intern REQ-12345") == normalize.title_norm("SWE Intern")


class TestCompanyNorm:
    ALIASES = {"hrt": "hudson river trading", "quantic": "walleye capital"}

    def test_legal_suffixes_stripped(self):
        assert normalize.company_norm("Acme, Inc.") == normalize.company_norm("Acme")
        assert normalize.company_norm("Susquehanna International Group, LLP").startswith(
            "susquehanna"
        )

    def test_alias_exact_match_only(self):
        assert normalize.company_norm("HRT", self.ALIASES) == "hudson river trading"
        # Exact full-string lookup — no substring logic.
        assert normalize.company_norm("HRT Europe", self.ALIASES) == "hrt europe"

    def test_citadel_family_never_merges(self):
        # SPEC §6.1 regression: separate firms, separate norms.
        a = normalize.company_norm("Citadel", self.ALIASES)
        b = normalize.company_norm("Citadel Securities", self.ALIASES)
        assert a != b


class TestPrefilter:
    def test_truth_table_full_policy(self):
        # SPEC §6.2 pinned truth table (the v1.0 'fail ALL' inversion regression).
        assert normalize.keep("Software Engineering Intern", "full") is True
        assert normalize.keep("Fall 2026 Software Co-op", "full") is False
        assert normalize.keep("Senior Barista", "full") is False
        assert normalize.keep("Winter 2026 Off-Cycle Intern", "full") is False

    def test_plurals_and_quant_titles_kept(self):
        assert normalize.keep("2027 Software Engineering Internships", "full") is True
        assert normalize.keep("Quantitative Trading Summer Analyst", "full") is True
        assert normalize.keep("Co-ops in Hardware", "full") is True

    def test_2026_in_description_never_checked(self):
        # Year excludes are title-only; descriptions carry 2026 deadlines.
        assert normalize.keep("SWE Intern (Summer 2027)", "full") is True

    def test_2027_rescues_a_2026_mention_in_title(self):
        assert normalize.keep("Winter 2026/2027 Trading Intern", "full") is True

    def test_excludes_only_policy_keeps_hn_shaped_rows(self):
        # HN title = first comment line; Reddit titles lack 'intern' sometimes.
        assert normalize.keep("Acme | NYC | Backend roles", "excludes_only") is True
        assert normalize.keep("Citadel 2027 SWE thread", "excludes_only") is True
        # Hard excludes still apply.
        assert normalize.keep("Fall 2026 co-op openings", "excludes_only") is False

    def test_internal_titles_dropped(self):
        assert normalize.keep("Internal Audit Manager", "full") is False
        assert normalize.keep("Internal Tools Engineer", "full") is False


class TestDescription:
    def test_middle_out_truncation(self):
        text = ("H" * 9000) + "MIDDLE" + ("T" * 9000)
        out = normalize.truncate_description(text)
        assert len(out) <= normalize.DESCRIPTION_CAP
        assert out.startswith("H")
        assert out.endswith("T")
        assert "[...truncated...]" in out

    def test_short_descriptions_untouched(self):
        assert normalize.truncate_description("short") == "short"
        assert normalize.truncate_description(None) is None

    def test_strip_html_unescapes_greenhouse_content(self):
        # Greenhouse content arrives entity-escaped (SPEC §5.2).
        raw = "&lt;div&gt;C++ &amp;amp; Python internship&lt;/div&gt;"
        out = normalize.strip_html(raw)
        assert "<div>" not in out and "&lt;" not in out
        assert "C++ & Python internship" in out


class TestTimestamps:
    def test_iso_norm_converts_offsets_to_utc_z(self):
        assert normalize.iso_norm("2026-07-13T11:39:48-04:00") == "2026-07-13T15:39:48Z"
        assert normalize.iso_norm(None) is None

    def test_epoch_conversions(self):
        assert normalize.iso_from_epoch_ms(1711403416463).startswith("2024-03-25T")
        assert normalize.iso_from_epoch_s(1711403416).startswith("2024-03-25T")

    def test_early_talent_program_titles_kept(self):
        # Sophomore programs aren't titled "Intern" (Citadel Launch, MS Explore...).
        assert normalize.keep("Launch Program - Sophomore Software Engineering", "full")
        assert normalize.keep("Optiver FutureFocus 2027", "full")
        assert normalize.keep("First-Year Explore Program", "full")
        assert normalize.keep("Women in Trading Tech Winternship", "full")
        assert normalize.keep("Rising Fellows Fellowship", "full")
        assert not normalize.keep("Senior Launch Manager", "full")

    def test_newgrad_target_swaps_include_regex(self):
        assert normalize.keep("Software Engineer, New Grad (2027)", "full", target="newgrad")
        assert normalize.keep("Graduate Software Engineer", "full", target="newgrad")
        assert normalize.keep("Entry-Level Backend Engineer", "full", target="newgrad")
        assert not normalize.keep("Software Engineering Intern", "full", target="newgrad")
        assert not normalize.keep("Senior Software Engineer", "full", target="newgrad")
