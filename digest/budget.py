"""Reserve bounded HOT/CVE work without letting an unprocessed backlog block news."""
from __future__ import annotations

from dataclasses import dataclass

from .gemini import HOT_CHUNK_SIZE


@dataclass(frozen=True)
class BudgetPlan:
    news_calls: int
    hot_calls: int
    cve_calls: int
    hot_candidates: int
    ranking_fits: bool = True

    @property
    def reserved_calls(self) -> int:
        return self.hot_calls + self.cve_calls


def hot_round_calls(candidate_count: int) -> int:
    calls = 1
    while candidate_count > HOT_CHUNK_SIZE:
        groups = (candidate_count + HOT_CHUNK_SIZE - 1) // HOT_CHUNK_SIZE
        calls += groups
        candidate_count = groups * 10
    return calls


def plan_api_budget(remaining_calls: int, current_candidates: int,
                    pending_candidates: int, batch_size: int,
                    cve_calls: int = 0, max_new_articles: int = 0,
                    hot_pending_candidates: int | None = None) -> BudgetPlan:
    """Bound HOT candidates by summaries that can finish within this run's budget.

    Every possible news call may successfully summarize a full batch. Each HOT
    round reserves three attempts, including the alternate model. Calls left
    over at a HOT group boundary stay reserved so the caller cannot start one
    extra news batch that would invalidate the candidate bound.

    With at least five calls, news gets one call even when all completed HOT
    candidates cannot fit the worst-case ranking budget. In that exceptional
    case ranking_fits is false, CVE work is deferred, and the API client's hard
    call limit still applies. Tiny budgets keep the existing four-call HOT
    reserve and do not start news work.
    """
    budget = max(0, int(remaining_calls))
    current = max(0, int(current_candidates))
    pending = max(0, int(pending_candidates))
    batch = max(1, int(batch_size))
    cve_requested = max(0, int(cve_calls))
    if max_new_articles > 0:
        pending = min(pending, int(max_new_articles))
    hot_pending = pending if hot_pending_candidates is None else min(pending, max(0, int(hot_pending_candidates)))

    if budget <= 4:
        return BudgetPlan(0, budget, 0, current,
                          budget >= hot_round_calls(current) * 3)

    minimum_hot = max(4, hot_round_calls(current) * 3)
    # Defer some CVE summaries if necessary to leave room for news progress.
    cve_reserved = min(cve_requested, max(0, budget - minimum_hot - 1))
    for news in range(budget - cve_reserved - 4, 0, -1):
        candidates = current + min(hot_pending, news * batch)
        hot_required = max(4, hot_round_calls(candidates) * 3)
        if news + hot_required + cve_reserved <= budget:
            return BudgetPlan(news, budget - news - cve_reserved,
                              cve_reserved, candidates)

    # Ranking all existing candidates may itself exceed the remaining budget.
    # Keep collection moving rather than growing an indefinitely blocked queue.
    return BudgetPlan(1, budget - 1, 0, current + min(hot_pending, batch), False)
