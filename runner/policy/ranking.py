"""How a rule's preference orders the substrates it permits (layer L2).

Shared by the placement engine, which chooses, and the compliance auditor,
which re-derives the choice from the ledger: one definition, so the two cannot
disagree about what "the rule's first choice" means.
"""

from __future__ import annotations

from collections.abc import Callable

from runner.policy.models import Substrate

__all__ = ["rank_key"]


def rank_key(prefer: str, order: dict[str, int]) -> Callable[[Substrate], tuple[float, ...]]:
    """Sort key for a preference, with the rule's order as the tie-break.

    The tie-break is not cosmetic: without it, two substrates with identical
    cost would be chosen by dictionary order, and the same policy would place
    the same step differently across processes. Reproducibility is a property an
    auditor tests.
    """
    if prefer == "cost":
        return lambda s: (s.cost_per_mtok, float(order.get(s.id, 999)))
    if prefer == "quality":
        return lambda s: (-float(s.quality), float(order.get(s.id, 999)))
    if prefer == "latency":
        # Latency is observed per call, not declared; until there is a
        # measurement, distance is the honest proxy — a nearer substrate is
        # rarely slower, and pretending to know better would be fiction.
        return lambda s: (float(s.distance), float(order.get(s.id, 999)))
    return lambda s: (float(s.distance), s.cost_per_mtok, float(order.get(s.id, 999)))
