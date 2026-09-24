"""A query inspection searches past the budgets that bound an unfiltered observation.

Only COM and window calls are substituted; uia.observe/_walk, the native driver's observe,
the broker inspect path, and caller_view are real.
"""

from types import SimpleNamespace

from jev_desktop.broker import Broker, BrokerConfig
from jev_desktop.contracts import SCOPE_LIMITS, AppRef, Geometry, Rect, WindowInfo
from jev_desktop.drivers.windows import uia
from jev_desktop.drivers.windows.native import NativeWindowsDriver
from jev_desktop.runtime import caller_view

APP_REF = "app:" + "a" * 24
WINDOW_REF = "win:" + "b" * 24
GEOMETRY = Geometry(1, 0, 0, 1000, 1000, 96, 1)
ROOT, DENY = ("pane", 0), ("button", "Deny")


def _fake_window(monkeypatch, tree):
    """`tree` maps a node to its children; a node is (role, label) and offscreen when role is "offscreen"."""
    monkeypatch.setattr(uia.win32, "foreground_window", lambda: 1)
    for name, value in {
        "owner_window": 0,
        "window_process_id": 1,
        "window_title": "Unbound",
        "window_class": "Chrome_WidgetWin_1",
        "dpi_for_window": 96,
    }.items():
        monkeypatch.setattr(uia.win32, name, lambda _, value=value: value)
    monkeypatch.setattr(uia.win32, "window_rect", lambda _: Rect(0, 0, 1000, 1000))
    monkeypatch.setattr(
        uia.win32, "user32", SimpleNamespace(IsWindowEnabled=lambda _: True, IsWindowVisible=lambda _: True)
    )

    def role(node):
        return "text" if node[0] == "offscreen" else node[0]

    def cached(node, prop):
        return {
            uia.PROP_NAME: "" if node == ROOT else str(node[1]),
            uia.PROP_ENABLED: True,
            uia.PROP_OFFSCREEN: node[0] == "offscreen",
            uia.PROP_BOUNDS: (10, 10, 50, 20),
        }.get(prop)

    monkeypatch.setattr(uia, "_control_type", role)
    monkeypatch.setattr(uia, "_cached", cached)
    monkeypatch.setattr(uia, "_available_patterns", lambda node: {"invoke": role(node) == "button"})
    monkeypatch.setattr(uia, "_runtime_id", lambda node: (hash(node),))
    visited = []

    def children(node):
        for kid in tree.get(node, ()):
            visited.append(kid)
            yield kid

    worker = SimpleNamespace(
        submit=lambda fn: fn(worker),
        element_from_handle=lambda _: ROOT,
        children=children,
        has_cached_walker=True,
        _poisoned=False,
    )
    return worker, visited


def _observe(worker, registry, *, query):
    return uia.observe(
        worker,
        registry,
        app_ref=APP_REF,
        process_id=1,
        scope_windows=[(WINDOW_REF, 1)],
        max_elements=SCOPE_LIMITS["max_elements"],
        max_depth=24,
        text_limit=80,
        include_invisible=False,
        geometry=GEOMETRY,
        query=query,
    )


def _offscreen_then_deny(count):
    return {ROOT: [*(("offscreen", f"off {i}") for i in range(count)), DENY]}


def test_query_finds_a_button_behind_offscreen_nodes_that_exhaust_the_unfiltered_budget(monkeypatch):
    # The report: ~1,600 offscreen nodes ahead of the card exhausted the 2,000-node budget.
    worker, _ = _fake_window(monkeypatch, _offscreen_then_deny(2000))
    registry = uia.Registry()
    snapshot = _observe(worker, registry, query="Deny")
    view = caller_view(snapshot, limit=5, query="Deny")
    assert [element["name"] for element in view["elements"]] == ["Deny"]
    assert "traversal node budget reached" not in view["truncation"]
    deny_id = view["elements"][0]["element_id"]
    assert uia.resolve_element(registry, deny_id, snapshot.snapshot_id, APP_REF).name == "Deny"
    assert set(registry.elements) == {deny_id}, "non-matching nodes are walked, not registered"


def test_query_finds_a_button_behind_more_visible_elements_than_the_element_cap(monkeypatch):
    buttons = [("button", f"Allow {i}") for i in range(600)]
    worker, _ = _fake_window(monkeypatch, {ROOT: [*buttons, DENY]})
    snapshot = _observe(worker, uia.Registry(), query="Deny")
    view = caller_view(snapshot, limit=5, query="Deny")
    assert [element["name"] for element in view["elements"]] == ["Deny"]
    assert view["truncation"] == []


def test_query_finds_a_row_past_the_collection_row_budget(monkeypatch):
    rows = [("listitem", f"file {i}") for i in range(600)]
    worker, _ = _fake_window(monkeypatch, {ROOT: [("list", "Files")], ("list", "Files"): rows})
    snapshot = _observe(worker, uia.Registry(), query="file 550")
    view = caller_view(snapshot, limit=5, query="file 550")
    assert [element["name"] for element in view["elements"]] == ["file 550"]


def test_query_search_stops_at_its_node_budget(monkeypatch):
    worker, visited = _fake_window(monkeypatch, _offscreen_then_deny(uia.QUERY_NODE_BUDGET))
    snapshot = _observe(worker, uia.Registry(), query="Deny")
    assert snapshot.elements == ()
    assert "traversal node budget reached" in snapshot.truncation
    assert DENY not in visited


def test_query_search_stops_at_its_time_limit(monkeypatch):
    monkeypatch.setattr(uia, "QUERY_SECONDS", 0.0)
    worker, _ = _fake_window(monkeypatch, _offscreen_then_deny(10))
    snapshot = _observe(worker, uia.Registry(), query="Deny")
    assert snapshot.elements == ()
    assert "query search time limit reached" in snapshot.truncation


def test_broker_inspect_query_returns_an_actionable_match_behind_offscreen_nodes(monkeypatch, tmp_path):
    worker, _ = _fake_window(monkeypatch, _offscreen_then_deny(2000))
    driver = NativeWindowsDriver(evidence_dir=tmp_path / "evidence")
    window = WindowInfo(
        window_ref=WINDOW_REF,
        app_ref=APP_REF,
        title="Unbound",
        class_name="Chrome_WidgetWin_1",
        process_id=1,
        modal=False,
        owner_window_ref=None,
        focused=True,
        visible=True,
        enabled=True,
        rect=Rect(0, 0, 1000, 1000),
        scope="scoped",
    )
    app = AppRef(
        app_ref=APP_REF,
        package_identity=None,
        executable_path="C:\\Program Files\\Unbound\\Unbound.exe",
        process_id=1,
        process_creation_time=1.0,
        window_refs=(WINDOW_REF,),
    )
    driver.worker = worker
    driver._started = True
    driver.registry.windows[WINDOW_REF] = uia.WindowHandle(WINDOW_REF, 1, APP_REF, 1)
    driver.discover = lambda app_ref=None: ([app], [window])
    driver._observe_windows = lambda app_ref: [window]
    driver.process_id_for = lambda app_ref: 1
    driver.screen = SimpleNamespace(geometry=lambda: GEOMETRY)
    broker = Broker(
        BrokerConfig(home=tmp_path, evidence_dir=tmp_path / "evidence", journal_path=tmp_path / "journal.sqlite"),
        driver=driver,
    )
    try:
        observed = broker._m_inspect(
            None, {"app_ref": APP_REF, "query": "Deny", "screenshot": False, "scope": {"max_elements": 5}}
        )
    finally:
        broker.journal.close()
    assert [element["name"] for element in observed["elements"]] == ["Deny"]
    deny_id = observed["elements"][0]["element_id"]
    assert uia.resolve_element(driver.registry, deny_id, observed["snapshot_id"], APP_REF).name == "Deny"
