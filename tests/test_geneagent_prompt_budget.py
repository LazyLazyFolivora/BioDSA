"""Tool responses stay small enough to be reread on every verification turn."""

import pytest

from biodsa.utils.prompt_budget import truncate_middle_chars

LIMIT = 6000


def test_short_result_is_passed_through_unchanged():
    text = "GBA1 is associated with Parkinson disease (PMID:12345)."
    assert truncate_middle_chars(text, LIMIT) == text


def test_result_at_the_limit_is_not_truncated():
    text = "x" * LIMIT
    assert truncate_middle_chars(text, LIMIT) == text


def test_long_result_is_bounded():
    # The marker adds a little, but the payload must not scale with the input.
    assert len(truncate_middle_chars("x" * 500_000, LIMIT)) < LIMIT + 200


def test_both_ends_survive_truncation():
    head = "FIRST ROW: SNCA, count 412"
    tail = "LAST ROW: total 1284 publications"
    text = head + "filler " * 5000 + tail

    shortened = truncate_middle_chars(text, LIMIT)

    assert shortened.startswith(head)
    assert shortened.endswith(tail)


def test_middle_is_what_gets_dropped():
    text = "A" * 4000 + "MIDDLE_MARKER" + "B" * 4000

    assert "MIDDLE_MARKER" not in truncate_middle_chars(text, LIMIT)


def test_omitted_length_accounts_for_the_whole_input():
    text = "".join(str(i % 10) for i in range(40_000))

    shortened = truncate_middle_chars(text, LIMIT)
    omitted = int(shortened.split("[")[1].split(" characters")[0])
    kept = len(shortened.split("...")[0].rstrip()) + len(
        shortened.rsplit("...", 1)[1].lstrip()
    )

    # Whoever reads the marker should be able to add what was kept to what was
    # reported omitted and land on the original size.
    assert kept + omitted == len(text)


@pytest.mark.parametrize("value", [None, 42, {"rows": []}, ["a", "b"]])
def test_non_string_results_are_rendered(value):
    # Tools return strings today, but a dict reaching this helper is far better
    # than a TypeError killing a verification worker mid-claim.
    assert truncate_middle_chars(value, LIMIT) == str(value)
