"""Bounded native observation with only COM and window calls substituted."""

from types import SimpleNamespace

import pytest

from jev_desktop.contracts import Geometry, Rect
from jev_desktop.drivers.windows import uia


def test_wide_observation_releases_discarded_handles_and_tracks_focus(monkeypatch):
    foreground = [1]
    visited = []
    monkeypatch.setattr(uia.win32, "foreground_window", lambda: foreground[0])
    for name, value in {
        "owner_window": 0,
        "window_process_id": 1,
        "window_title": "window",
        "window_class": "window",
        "dpi_for_window": 96,
    }.items():
        monkeypatch.setattr(uia.win32, name, lambda _, value=value: value)
    monkeypatch.setattr(uia.win32, "window_rect", lambda _: Rect(0, 0, 100, 100))
    monkeypatch.setattr(
        uia.win32, "user32", SimpleNamespace(IsWindowEnabled=lambda _: True, IsWindowVisible=lambda _: True)
    )
    monkeypatch.setattr(uia, "_control_type", lambda node: "listitem" if node else "button")
    monkeypatch.setattr(
        uia,
        "_cached",
        lambda node, prop: {
            uia.PROP_NAME: str(node),
            uia.PROP_ENABLED: True,
            uia.PROP_OFFSCREEN: node == 1,
            uia.PROP_BOUNDS: (0, 0, 20, 20),
        }.get(prop),
    )
    monkeypatch.setattr(uia, "_runtime_id", lambda node: (node,))
    monkeypatch.setattr(uia, "point_hits_element", lambda _, node, *_point: node == 1)

    def children(node):
        if node == 0:
            for index in range(1, 1001):
                visited.append(index)
                yield index

    worker = SimpleNamespace(
        submit=lambda fn: fn(worker), element_from_handle=lambda _: 0, children=children, has_cached_walker=True
    )
    registry = uia.Registry()

    def observe():
        return uia.observe(
            worker,
            registry,
            app_ref="app",
            process_id=1,
            scope_windows=[("foreground", 1), ("background", 2)],
            max_elements=20,
            max_depth=12,
            text_limit=80,
            include_invisible=False,
            geometry=Geometry(1, 0, 0, 100, 100, 96, 1),
        )

    first = observe()
    second = observe()
    assert len(registry.elements) == len(second.elements) <= 20
    assert len(visited) <= 160
    assert first.fingerprint == second.fingerprint
    assert second.elements[0].window_ref == "foreground"
    foreground[0] = 2
    assert observe().fingerprint != second.fingerprint
    assert second.truncation
    assert any(e.name == "1" and e.visible for e in second.elements)


def _observe_tree(monkeypatch, tree, roles, onscreen, max_elements, hit_tests=None):
    visited = []
    for name, value in {
        "owner_window": 0,
        "window_process_id": 1,
        "window_title": "window",
        "window_class": "window",
        "dpi_for_window": 96,
        "foreground_window": 1,
    }.items():
        monkeypatch.setattr(uia.win32, name, lambda *_, value=value: value)
    monkeypatch.setattr(uia.win32, "window_rect", lambda _: Rect(0, 0, 100, 100))
    monkeypatch.setattr(
        uia.win32, "user32", SimpleNamespace(IsWindowEnabled=lambda _: True, IsWindowVisible=lambda _: True)
    )
    monkeypatch.setattr(uia, "_control_type", lambda node: roles.get(node, "group" if "line" not in node else "text"))
    monkeypatch.setattr(
        uia,
        "_cached",
        lambda node, prop: {
            uia.PROP_NAME: node,
            uia.PROP_ENABLED: True,
            uia.PROP_OFFSCREEN: node not in onscreen,
            uia.PROP_BOUNDS: (0, 0, 20, 20),
        }.get(prop),
    )
    monkeypatch.setattr(uia, "_runtime_id", lambda node: (hash(node),))
    hit_tests = [] if hit_tests is None else hit_tests
    monkeypatch.setattr(uia, "point_hits_element", lambda *_: hit_tests.append(1) or False)

    def children(node):
        for child in tree.get(node, []):
            visited.append(child)
            yield child

    worker = SimpleNamespace(
        submit=lambda fn: fn(worker), element_from_handle=lambda _: "root", children=children, has_cached_walker=True
    )
    snapshot = uia.observe(
        worker,
        uia.Registry(),
        app_ref="app",
        process_id=1,
        scope_windows=[("window", 1)],
        max_elements=max_elements,
        max_depth=12,
        text_limit=80,
        include_invisible=False,
        geometry=Geometry(1, 0, 0, 100, 100, 96, 1),
    )
    return {element.name for element in snapshot.elements}, visited


@pytest.mark.parametrize("card_role", ["group", "listitem"])
def test_offscreen_subtrees_do_not_spend_the_budget_before_later_onscreen_controls(monkeypatch, card_role):
    # Board fixture shape: offscreen cards precede an onscreen right rail; one offscreen row
    # holds a position:fixed child that Chromium reports onscreen.
    tree = {
        "root": ["Board", "Details"],
        "Board": ["Row with fixed child", *(f"Card {i}" for i in range(30))],
        "Row with fixed child": ["Fixed button"],
        **{f"Card {i}": [f"Card {i} line {j}" for j in range(10)] for i in range(30)},
        "Details": ["View All"],
    }
    roles = {"root": "pane", "Board": "group", "Details": "group", "Fixed button": "button", "View All": "button"}
    roles.update({f"Card {i}": card_role for i in range(30)})
    onscreen = {"root", "Board", "Details", "Fixed button", "View All"}
    names, visited = _observe_tree(monkeypatch, tree, roles, onscreen, max_elements=60)
    assert {"View All", "Fixed button"} <= names
    assert len(visited) <= 240 + 240 // 4  # the node budget plus the deferred subtrees' extra quarter


def test_a_spent_row_budget_does_not_drop_the_rest_of_an_offscreen_subtree(monkeypatch):
    tree = {
        "root": ["List", "Board"],
        "List": [f"Item {i}" for i in range(11)],
        "Board": ["Fixed row", "Fixed button"],
    }
    roles = {"root": "pane", "List": "list", "Fixed row": "listitem", "Fixed button": "button"}
    roles.update({f"Item {i}": "listitem" for i in range(11)})
    onscreen = {"root", "List", "Fixed row", "Fixed button", *(f"Item {i}" for i in range(11))}
    names, _ = _observe_tree(monkeypatch, tree, roles, onscreen, max_elements=10)
    assert "Item 10" not in names
    assert "Fixed button" in names


def test_a_collection_inside_offscreen_content_waits_behind_later_offscreen_siblings(monkeypatch):
    tree = {
        "root": ["Board", "Rail"],
        "Board": ["Archive"],
        "Archive": [f"Card {i}" for i in range(80)],
        "Rail": ["Fixed button"],
    }
    roles = {"root": "pane", "Archive": "list", "Fixed button": "button"}
    names, _ = _observe_tree(monkeypatch, tree, roles, {"root", "Fixed button"}, max_elements=10)
    assert "Fixed button" in names


def test_offscreen_rows_do_not_spend_the_row_budget_of_a_visible_row(monkeypatch):
    tree = {
        "root": ["Panel", "List"],
        "Panel": ["Fixed row"],
        "List": [f"Item {i}" for i in range(11)],
    }
    roles = {"root": "pane", "List": "list", "Fixed row": "listitem"}
    roles.update({f"Item {i}": "listitem" for i in range(11)})
    names, _ = _observe_tree(monkeypatch, tree, roles, {"root", "List", "Fixed row"}, max_elements=10)
    assert "Fixed row" in names


@pytest.mark.parametrize("list_onscreen", [False, True], ids=["offscreen-list", "offscreen-rows"])
def test_offscreen_list_content_does_not_starve_a_later_fixed_control(monkeypatch, list_onscreen):
    tree = {"root": ["Archive", "Rail"], "Archive": [f"Card {i}" for i in range(80)], "Rail": ["Fixed button"]}
    roles = {"root": "pane", "Archive": "list", "Fixed button": "button"}
    roles.update({f"Card {i}": "listitem" for i in range(80)})
    onscreen = {"root", "Fixed button", *(["Archive"] if list_onscreen else [])}
    names, _ = _observe_tree(monkeypatch, tree, roles, onscreen, max_elements=10)
    assert "Fixed button" in names


def test_offscreen_rows_keep_later_siblings_outside_a_list(monkeypatch):
    rows = [f"Card {i}" for i in range(11)]
    tree = {"root": ["Board"], "Board": [*rows, "Fixed button"]}
    roles = {"root": "pane", "Fixed button": "button", **dict.fromkeys(rows, "listitem")}
    names, _ = _observe_tree(monkeypatch, tree, roles, {"root", "Fixed button"}, max_elements=10)
    assert "Fixed button" in names


def test_deferred_offscreen_content_keeps_a_share_of_the_node_budget(monkeypatch):
    tree = {"root": ["Board", "List"], "Board": ["Fixed button"], "List": [f"Card {i}" for i in range(80)]}
    roles = {"root": "pane", "List": "list", "Fixed button": "button"}
    names, _ = _observe_tree(monkeypatch, tree, roles, {"root", "List", "Fixed button"}, max_elements=10)
    assert "Fixed button" in names


def test_queued_offscreen_content_does_not_cut_short_the_onscreen_walk(monkeypatch):
    leaves = [f"Hidden {i}" for i in range(50)]
    tree = {"root": [*leaves, "Button"]}
    roles = {"root": "pane", "Button": "button"}
    names, _ = _observe_tree(monkeypatch, tree, roles, {"root", "Button"}, max_elements=10)
    assert "Button" in names


def test_deferred_subtrees_take_turns_with_the_extra_budget(monkeypatch):
    tree = {"root": ["Board", "Rail"], "Board": [f"Hidden {i}" for i in range(100)], "Rail": ["Fixed button"]}
    roles = {"root": "pane", "Fixed button": "button"}
    names, _ = _observe_tree(monkeypatch, tree, roles, {"root", "Fixed button"}, max_elements=10)
    assert "Fixed button" in names


def test_a_fixed_child_of_a_late_offscreen_row_is_observed(monkeypatch):
    rows = [f"Card {i}" for i in range(11)]
    tree = {"root": ["List"], "List": rows, "Card 10": ["Fixed button"]}
    roles = {"root": "pane", "List": "list", "Fixed button": "button", **dict.fromkeys(rows, "listitem")}
    names, _ = _observe_tree(monkeypatch, tree, roles, {"root", "List", "Fixed button"}, max_elements=10)
    assert "Fixed button" in names


def test_row_hit_tests_stay_within_the_row_budget(monkeypatch):
    rows = [f"Row {i}" for i in range(500)]
    tree = {"root": ["List"], "List": rows}
    roles = {"root": "pane", "List": "list", **dict.fromkeys(rows, "listitem")}
    hit_tests: list[int] = []
    _observe_tree(monkeypatch, tree, roles, {"root", "List"}, max_elements=10, hit_tests=hit_tests)
    assert len(hit_tests) <= 10


def test_scope_bounds_have_local_ceilings():
    import pytest

    from jev_desktop.contracts import SCOPE_LIMITS, ContractError, ScopeSpec

    app_ref = "app:" + "a" * 24
    assert ScopeSpec.from_json({"app_ref": app_ref, **SCOPE_LIMITS}).max_elements == SCOPE_LIMITS["max_elements"]
    for key, ceiling in SCOPE_LIMITS.items():
        with pytest.raises(ContractError):
            ScopeSpec.from_json({"app_ref": app_ref, key: ceiling + 1})


def test_scroll_notches_are_bounded_integers():
    import pytest

    from jev_desktop.contracts import ContractError, _scroll

    assert _scroll({"notches": -3}) == {"notches": -3}
    for bad in ({"notches": 51}, {"amount": -51}, {"notches": 1.5}, {"notches": True}):
        with pytest.raises(ContractError):
            _scroll(bad)


def test_task_text_names_must_be_unique_across_plain_and_secret_inputs():
    import pytest

    from jev_desktop.broker import Broker
    from jev_desktop.contracts import ContractError

    task = {"goal": "g", "texts": {"password": "a"}, "secret_texts": {"password": "b"}, "window_refs": ["w"]}
    with pytest.raises(ContractError, match="uniquely named"):
        Broker._run_task(SimpleNamespace(), None, {"task": task})
    too_many = {"texts": {f"t{i}": "x" for i in range(9)}, "secret_texts": {f"s{i}": "x" for i in range(8)}}
    with pytest.raises(ContractError, match="at most 16"):
        Broker._run_task(SimpleNamespace(), None, {"task": {**task, **too_many}})


def test_owned_dialog_scope_excludes_unrelated_windows():
    from jev_desktop.contracts import ScopeSpec
    from jev_desktop.drivers.windows.native import NativeWindowsDriver

    windows = [
        SimpleNamespace(window_ref=ref, app_ref=app, owner_window_ref=owner)
        for ref, app, owner in [
            ("root", "app", None),
            ("dialog", "app", "root"),
            ("other", "app", None),
            ("foreign", "foreign", "root"),
        ]
    ]
    driver = NativeWindowsDriver.__new__(NativeWindowsDriver)
    scope = ScopeSpec(app_ref="app", window_refs=("root",))
    assert [w.window_ref for w in driver._scoped_windows(scope, windows)] == ["root", "dialog"]
