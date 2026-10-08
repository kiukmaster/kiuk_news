import pytest

from digest.budget import hot_round_calls, plan_api_budget


@pytest.mark.parametrize(('candidates', 'expected'), [(0, 1), (80, 1), (170, 4), (530, 8)])
def test_hot_round_budget_matches_bounded_ranking(candidates, expected):
    assert hot_round_calls(candidates) == expected


def test_large_backlog_does_not_reserve_every_news_call_for_hot():
    # Actual incident: six cached GitHub cards, 1,912 pending articles, and no
    # news summaries because the old reservation treated all pending as HOT.
    old_hot_reserve = hot_round_calls(6 + 1912) * 3
    assert min(80, old_hot_reserve + 12) == 80

    plan = plan_api_budget(80, 6, 1912, 6, cve_calls=12)
    assert plan.news_calls == 52
    assert plan.hot_candidates == 318
    assert plan.cve_calls == 12
    assert plan.hot_calls >= hot_round_calls(plan.hot_candidates) * 3
    assert plan.news_calls + plan.reserved_calls == 80
    assert plan.ranking_fits

    # More queued rows cannot change this run's possible completed candidates.
    assert plan_api_budget(80, 6, 100_000, 6, cve_calls=12) == plan


def test_hot_group_boundary_does_not_leave_an_unreserved_extra_news_call():
    plan = plan_api_budget(80, 6, 1912, 6, cve_calls=12)
    assert 80 - plan.reserved_calls == plan.news_calls
    extra_candidates = 6 + (plan.news_calls + 1) * 6
    assert plan.news_calls + 1 + hot_round_calls(extra_candidates) * 3 + 12 > 80


def test_existing_completed_candidates_are_included_in_ranking_reserve():
    plan = plan_api_budget(80, 323, 2000, 6, cve_calls=12)
    assert plan.news_calls > 0
    assert plan.hot_candidates == 323 + plan.news_calls * 6
    assert plan.hot_calls >= hot_round_calls(plan.hot_candidates) * 3
    assert plan.news_calls + plan.reserved_calls == 80


def test_configured_article_limit_reduces_possible_hot_candidates():
    plan = plan_api_budget(80, 6, 1912, 6, cve_calls=12, max_new_articles=12)
    assert plan.hot_candidates == 18
    assert plan.reserved_calls == 16
    assert plan.news_calls == 64


def test_pending_rows_bound_possible_new_candidates():
    plan = plan_api_budget(80, 6, 2, 6, cve_calls=12)
    assert plan.hot_candidates == 8
    assert plan.reserved_calls == 16


def test_independent_latest_summaries_do_not_expand_hot_ranking():
    plan = plan_api_budget(80, 70, 30, 6, cve_calls=6, hot_pending_candidates=10)
    assert plan.hot_candidates == 80
    assert plan.hot_calls == 4
    assert plan.news_calls + plan.reserved_calls == 80


@pytest.mark.parametrize('budget', range(5))
def test_tiny_budget_does_not_start_unfunded_news_batch(budget):
    plan = plan_api_budget(budget, 0, 1, 6)
    assert plan.news_calls == 0
    assert plan.reserved_calls == budget


def test_large_cve_reservation_still_allows_news_to_progress():
    plan = plan_api_budget(80, 0, 1912, 6, cve_calls=80)
    assert plan.news_calls == 1
    assert plan.hot_calls == 4
    assert plan.cve_calls == 75
    assert plan.news_calls + plan.reserved_calls == 80


def test_impossible_full_hot_budget_defers_cves_and_keeps_news_moving():
    plan = plan_api_budget(80, 10_000, 1912, 6, cve_calls=12)
    assert plan.news_calls == 1
    assert plan.cve_calls == 0
    assert not plan.ranking_fits
    assert plan.news_calls + plan.reserved_calls == 80
