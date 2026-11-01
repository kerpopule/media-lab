"""A storyboard stays a candidate until its owner approves it, whatever the flag's
older, owner-named spelling on boards written before 2026-09-25."""
from media_lab_core import cut


def test_candidate_flag_uses_the_owner_neutral_key():
    assert cut.CANDIDATE_KEY == "candidate_not_final_until_owner_approves"
    assert cut.candidate_until_approved({cut.CANDIDATE_KEY: True})


def test_candidate_flag_still_reads_boards_written_under_an_older_key():
    assert cut.candidate_until_approved({"candidate_not_final_until_director_approves": True})


def test_a_false_or_missing_flag_is_not_a_candidate():
    assert not cut.candidate_until_approved({cut.CANDIDATE_KEY: False})
    assert not cut.candidate_until_approved({"candidate_not_final_until_director_approves": "yes"})
    assert not cut.candidate_until_approved({})
