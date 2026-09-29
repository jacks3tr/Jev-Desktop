"""JD-32: provider-visible Chromium documents must belong to the rendered page."""

from types import SimpleNamespace as NS

import pytest

from jev_desktop.contracts import Geometry, Rect
from jev_desktop.drivers.windows import uia, win32


class Node:
    def __init__(self, identity, role, name, rect, *, parent=None, **properties):
        self.parent = parent
        self.children = []
        if parent is not None:
            parent.children.append(self)
        self.properties = {
            uia.PROP_RUNTIME_ID: [identity],
            uia.PROP_CONTROL_TYPE: role,
            uia.PROP_NAME: name,
            uia.PROP_BOUNDS: [rect.left, rect.top, rect.width, rect.height],
            uia.PROP_ENABLED: True,
            uia.PROP_OFFSCREEN: False,
            uia.PROP_FRAMEWORK: "Chrome",
        }
        self.properties.update({getattr(uia, key): value for key, value in properties.items()})

    def GetCachedPropertyValue(self, prop):
        return self.properties.get(prop)

    GetCurrentPropertyValue = GetCachedPropertyValue


@pytest.fixture
def browser(monkeypatch):
    bounds = Rect(0, 0, 100, 100)
    root = Node(1, 50032, "Edge", bounds)
    # Cached geometry from a sleeping tab is stale, nonempty, and wider than its window.
    sleeping = Node(2, 50030, "Sleeping page", Rect(0, 10, 150, 100), parent=root)
    for identity in range(3, 9):
        Node(identity, 50000, f"Hidden control {identity}", Rect(10, 20, 90, 40), parent=sleeping)
    active = Node(10, 50030, "Device sign-in", Rect(0, 10, 100, 100), parent=root)
    button = Node(11, 50000, "Continue", Rect(10, 20, 90, 40), parent=active)
    worker = NS(
        automation=NS(
            RawViewWalker=NS(GetParentElement=lambda element: element.parent),
            CompareElements=lambda left, right: left is right,
        ),
        has_cached_walker=True,
        element_from_handle=lambda hwnd: root,
        focused_element=lambda hwnd: None,
        children=lambda element: iter(element.children),
        element_at_point=lambda x, y: button if button_rect.contains(x, y) else active,
    )
    button_rect = Rect(10, 20, 90, 40)
    worker.submit = lambda fn, **kwargs: fn(worker)
    monkeypatch.setattr(win32, "window_rect", lambda hwnd: bounds)
    monkeypatch.setattr(win32, "window_title", lambda hwnd: "Edge")
    monkeypatch.setattr(win32, "window_class", lambda hwnd: "Chrome_WidgetWin_1")
    monkeypatch.setattr(win32, "window_process_id", lambda hwnd: 7)
    monkeypatch.setattr(win32, "owner_window", lambda hwnd: 0)
    monkeypatch.setattr(win32, "foreground_window", lambda: 1)
    monkeypatch.setattr(win32, "dpi_for_window", lambda hwnd: 96)
    monkeypatch.setattr(win32.user32, "IsWindowVisible", lambda hwnd: True)
    monkeypatch.setattr(win32.user32, "IsWindowEnabled", lambda hwnd: True)
    return NS(root=root, sleeping=sleeping, active=active, button=button, worker=worker, bounds=bounds)


def inspect(browser, **options):
    registry = uia.Registry()
    snapshot = uia.observe(
        browser.worker,
        registry,
        app_ref="app:edge",
        process_id=7,
        scope_windows=[("win:edge", 1)],
        max_elements=options.pop("max_elements", 50),
        max_depth=8,
        text_limit=100,
        include_invisible=options.pop("include_invisible", False),
        geometry=Geometry(1, 0, 0, 100, 100, 96, 1.0),
        **options,
    )
    assert {element.element_id for element in snapshot.elements} == set(registry.elements)
    return snapshot


def test_sleeping_document_cannot_spend_the_active_pages_element_budget(browser):
    snapshot = inspect(browser, max_elements=5)
    names = {element.name for element in snapshot.elements}
    assert {"Device sign-in", "Continue"} <= names
    assert "Sleeping page" not in names
    assert not any(name.startswith("Hidden control") for name in names)
    assert all(element.visible for element in snapshot.elements)
    assert all(text["name"] != "Sleeping page" for text in snapshot.context["texts"])


@pytest.mark.parametrize("query", ["Sleeping page", "Hidden control"])
def test_query_cannot_resurrect_a_sleeping_document_or_its_controls(browser, query):
    assert not inspect(browser, query=query).elements


def test_invisible_diagnostics_mark_the_entire_sleeping_subtree_invisible(browser):
    snapshot = inspect(browser, include_invisible=True)
    hidden = [
        element
        for element in snapshot.elements
        if element.name == "Sleeping page" or element.name.startswith("Hidden control")
    ]
    assert len(hidden) == 7
    assert all(not element.visible for element in hidden)
    assert all(element.visible for element in snapshot.elements if element.name in {"Device sign-in", "Continue"})


def test_focused_fast_path_does_not_bypass_hidden_document_ancestry(browser):
    hidden = browser.sleeping.children[0]
    hidden.properties[uia.PROP_FOCUSED] = True
    browser.worker.focused_element = lambda hwnd: hidden
    snapshot = inspect(browser, query="Hidden control")
    assert not snapshot.elements
    assert snapshot.context["focused_element_id"] is None


def test_document_samples_stay_inside_the_scoped_window(browser):
    def hit(x, y):
        assert browser.bounds.contains(x, y)
        return browser.active

    browser.worker.element_at_point = hit
    assert {"Device sign-in", "Continue"} <= {element.name for element in inspect(browser).elements}


def test_center_popover_does_not_hide_the_active_document(browser):
    popup_rect = Rect(30, 30, 70, 80)
    popup = Node(20, 50026, "Popover", popup_rect, parent=browser.root)
    Node(21, 50000, "Popover action", Rect(35, 35, 65, 55), parent=popup)
    browser.worker.element_at_point = lambda x, y: popup if popup_rect.contains(x, y) else browser.active
    names = {element.name for element in inspect(browser).elements}
    assert {"Device sign-in", "Continue", "Popover action"} <= names
    assert "Sleeping page" not in names


def test_dialog_inside_active_document_is_positive_visibility_evidence(browser):
    dialog = Node(20, 50026, "Sign-in dialog", Rect(0, 10, 100, 100), parent=browser.active)
    Node(21, 50000, "Dialog action", Rect(30, 30, 70, 60), parent=dialog)
    browser.worker.element_at_point = lambda x, y: dialog
    names = {element.name for element in inspect(browser).elements}
    assert {"Device sign-in", "Continue", "Dialog action"} <= names
    assert "Sleeping page" not in names


def test_embedded_document_in_the_active_page_is_retained(browser):
    frame = Node(20, 50030, "Embedded frame", Rect(20, 20, 80, 80), parent=browser.active)
    frame_button = Node(21, 50000, "Frame action", Rect(30, 30, 70, 60), parent=frame)
    browser.worker.element_at_point = lambda x, y: (
        frame_button if Rect(20, 20, 80, 80).contains(x, y) else browser.active
    )
    assert {"Device sign-in", "Embedded frame", "Frame action"} <= {
        element.name for element in inspect(browser).elements
    }


def test_owned_dialog_is_observed_against_its_own_window_bounds(browser, monkeypatch):
    dialog_bounds = Rect(110, 20, 210, 120)
    dialog = Node(20, 50032, "Native dialog", dialog_bounds)
    page = Node(21, 50030, "Dialog page", dialog_bounds, parent=dialog)
    Node(22, 50000, "Dialog action", Rect(120, 30, 200, 60), parent=page)
    monkeypatch.setattr(win32, "window_rect", lambda hwnd: dialog_bounds if hwnd == 2 else browser.bounds)
    monkeypatch.setattr(win32, "owner_window", lambda hwnd: 1 if hwnd == 2 else 0)
    browser.worker.element_from_handle = lambda hwnd: dialog if hwnd == 2 else browser.root
    browser.worker.element_at_point = lambda x, y: page if dialog_bounds.contains(x, y) else browser.active
    snapshot = uia.observe(
        browser.worker,
        uia.Registry(),
        app_ref="app:edge",
        process_id=7,
        scope_windows=[("win:edge", 1), ("win:dialog", 2)],
        max_elements=50,
        max_depth=8,
        text_limit=100,
        include_invisible=False,
        geometry=Geometry(1, 0, 0, 220, 140, 96, 1.0),
    )
    action = next(element for element in snapshot.elements if element.name == "Dialog action")
    assert action.visible and action.window_ref == "win:dialog"


def test_native_document_keeps_provider_visibility_semantics(browser, monkeypatch):
    monkeypatch.setattr(win32, "window_class", lambda hwnd: "NativeEditor")
    for node in (browser.root, browser.sleeping, browser.active):
        node.properties[uia.PROP_FRAMEWORK] = "Win32"
    browser.worker.element_at_point = lambda x, y: None
    assert "Sleeping page" in {element.name for element in inspect(browser).elements}


@pytest.mark.parametrize("failure", ["missing-hit", "provider-error"])
def test_unproven_chromium_document_is_not_reported_visible(browser, failure):
    def hit(x, y):
        if failure == "provider-error":
            raise RuntimeError("provider unavailable")
        return None

    browser.worker.element_at_point = hit
    snapshot = inspect(browser)
    assert not any(element.role == "document" or element.name == "Continue" for element in snapshot.elements)
    assert snapshot.truncation
