"""README.pl.md must describe the same features as README.md (the strong model writes the test, a worker translates)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# names of commands, options and fields that must appear verbatim in both files
TOKENS = ["red_check", "red_check_failed", "expect_red", "--no-red-check", "integration_test_cmd", "--integration-test",
          "orchestra ledger budget --set 1.0", "price_in_per_mtok", "price_out_per_mtok", "over_budget",
          "orchestra ledger budget --check", "orchestra ledger add RUN TASK --outcome accepted|fixed|rejected",
          "orchestra ledger show"]


def test_new_features_documented_in_both_languages():
    en = (ROOT / "README.md").read_text(encoding="utf-8")
    pl = (ROOT / "README.pl.md").read_text(encoding="utf-8")
    assert [t for t in TOKENS if t not in en] == []
    assert [t for t in TOKENS if t not in pl] == []


def test_polish_readme_has_no_em_or_en_dashes():
    assert not {"—", "–"} & set((ROOT / "README.pl.md").read_text(encoding="utf-8"))


def test_polish_bullets_sit_in_the_safeguards_section():
    pl = (ROOT / "README.pl.md").read_text(encoding="utf-8")
    start = pl.index("## Hierarchia kosztowa i zabezpieczenia")
    end = pl.index("\n## ", start + 5)
    section = pl[start:end]
    assert all(t in section for t in TOKENS)
