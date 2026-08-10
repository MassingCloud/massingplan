"""Topological order, cycle naming, and the driving-chain walk."""

from __future__ import annotations

import pytest

from massingplan.core.graph import (
    ScheduleCycleError,
    build_successors,
    driving_chain,
    find_cycle,
    topological_order,
    validate_network,
)


def test_a_logic_free_schedule_round_trips_in_declaration_order() -> None:
    """Seeded in declaration order, not set order.

    Seeding the ready queue from a set makes the output depend on hash ordering,
    so the same file lists its activities differently between runs and no diff
    of two exports is ever clean.
    """
    ids = ["C", "A", "B", "D"]
    assert topological_order(ids, []) == ids


def test_a_chain_orders_by_logic_not_by_name() -> None:
    ids = ["C", "B", "A"]
    edges = [("A", "B"), ("B", "C")]
    assert topological_order(ids, edges) == ["A", "B", "C"]


def test_a_diamond_keeps_both_branches_after_the_split() -> None:
    ids = ["START", "LEFT", "RIGHT", "END"]
    edges = [("START", "LEFT"), ("START", "RIGHT"), ("LEFT", "END"), ("RIGHT", "END")]
    order = topological_order(ids, edges)
    assert order[0] == "START"
    assert order[-1] == "END"
    assert set(order[1:3]) == {"LEFT", "RIGHT"}


def test_disconnected_components_all_appear() -> None:
    ids = ["A", "B", "X", "Y"]
    edges = [("A", "B"), ("X", "Y")]
    order = topological_order(ids, edges)
    assert sorted(order) == ["A", "B", "X", "Y"]
    assert order.index("A") < order.index("B")
    assert order.index("X") < order.index("Y")


def test_a_cycle_is_reported_as_the_loop_itself_with_no_lead_in_tail() -> None:
    """ "A cycle exists" is not actionable on a two-thousand-activity network."""
    ids = ["LEAD", "A", "B", "C"]
    edges = [("LEAD", "A"), ("A", "B"), ("B", "C"), ("C", "A")]
    with pytest.raises(ScheduleCycleError) as excinfo:
        topological_order(ids, edges)
    cycle = excinfo.value.cycle
    assert cycle[0] == cycle[-1], "the reported loop must close"
    assert set(cycle) == {"A", "B", "C"}
    assert "LEAD" not in cycle
    assert "A -> B -> C -> A" in str(excinfo.value)


def test_the_reported_cycle_is_the_same_one_every_time() -> None:
    """An error message that changes between runs is one nobody trusts."""
    ids = ["A", "B", "C", "D"]
    edges = [("A", "B"), ("B", "A"), ("C", "D"), ("D", "C")]
    first = None
    for _ in range(5):
        with pytest.raises(ScheduleCycleError) as excinfo:
            topological_order(ids, edges)
        if first is None:
            first = excinfo.value.cycle
        assert excinfo.value.cycle == first


def test_find_cycle_on_an_acyclic_remainder_returns_the_path_it_walked() -> None:
    successors = {"A": ["B"], "B": []}
    assert find_cycle(["A", "B"], successors) == ["A", "B"]


def test_find_cycle_of_nothing_is_nothing() -> None:
    assert find_cycle([], {}) == []


def test_build_successors_preserves_declaration_order() -> None:
    successors = build_successors(["A", "B", "C"], [("A", "C"), ("A", "B")])
    assert successors["A"] == ["C", "B"]
    assert successors["C"] == []


def test_driving_chain_walks_back_from_the_terminal_activity() -> None:
    driving = {"END": "MID", "MID": "START", "START": None}
    assert driving_chain(driving, "END") == ["START", "MID", "END"]


def test_driving_chain_of_no_terminal_is_empty() -> None:
    assert driving_chain({}, None) == []


def test_driving_chain_stops_rather_than_hanging_if_an_invariant_broke() -> None:
    """Driving links cannot loop in a network that passed the topological sort."""
    driving = {"A": "B", "B": "A"}
    chain = driving_chain(driving, "A")
    assert chain == ["B", "A"]


def test_validate_rejects_a_duplicate_id() -> None:
    with pytest.raises(ValueError, match="Duplicate activity ids"):
        validate_network(["A", "B", "A"], [])


def test_validate_rejects_a_dangling_reference_naming_the_side() -> None:
    with pytest.raises(ValueError, match="unknown predecessor"):
        validate_network(["A"], [("GHOST", "A")])
    with pytest.raises(ValueError, match="unknown successor"):
        validate_network(["A"], [("A", "GHOST")])


def test_validate_names_a_self_loop_specifically() -> None:
    """Kahn would report this as an ordinary cycle, which is a less useful message."""
    with pytest.raises(ValueError, match="depends on itself"):
        validate_network(["A"], [("A", "A")])


def test_validate_passes_a_sound_network() -> None:
    validate_network(["A", "B"], [("A", "B")])
