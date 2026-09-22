"""Bounded native observation with only COM and window calls substituted."""

from types import SimpleNamespace

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
