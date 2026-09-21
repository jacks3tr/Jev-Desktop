"""Dispatch-journal guarantees: single-use identities, no automatic replay, recovery."""

from __future__ import annotations

import pytest

from jev_desktop.contracts import (
    ContractError,
    DispatchState,
    DriverError,
    Pause,
    Reason,
    Receipt,
    UncertainEffect,
)
from jev_desktop.journal import DispatchJournal, JournalUnhealthy


def receipt(action_id: str) -> Receipt:
    return Receipt(
        action_id=action_id,
        dispatch_state=DispatchState.DISPATCHED,
        mechanism=__import__(
            "jev_desktop.contracts", fromlist=["DispatchMechanism"]
        ).DispatchMechanism.SEND_INPUT_MOUSE,
        inserted_events=2,
        started_at=1.0,
        finished_at=1.1,
        target={"element_id": "el:" + "0" * 24},
        notes=("first",),
    )


@pytest.fixture()
def journal(tmp_path):
    store = DispatchJournal(str(tmp_path / "journal.sqlite"))
    yield store
    store.close()


def test_duplicate_completed_action_returns_the_original_receipt(journal):
    action_id = "act:" + "1" * 24
    sends = []

    def send():
        sends.append(1)
        return receipt(action_id)

    first = journal.dispatch_once(
        action_id=action_id, request_hash="hash-1", run_id="run:" + "a" * 24, guard=lambda: None, send=send
    )
    second = journal.dispatch_once(
        action_id=action_id, request_hash="hash-1", run_id="run:" + "a" * 24, guard=lambda: None, send=send
    )
    assert len(sends) == 1, "native dispatch must happen exactly once"
    assert first.to_json() == second.to_json()


def test_same_key_with_different_binding_is_rejected(journal):
    action_id = "act:" + "2" * 24
    journal.dispatch_once(
        action_id=action_id,
        request_hash="hash-1",
        run_id="run:" + "a" * 24,
        guard=lambda: None,
        send=lambda: receipt(action_id),
    )
    with pytest.raises(ContractError):
        journal.dispatch_once(
            action_id=action_id,
            request_hash="hash-2",
            run_id="run:" + "a" * 24,
            guard=lambda: None,
            send=lambda: receipt(action_id),
        )


def test_guard_rejection_records_not_dispatched_and_never_sends(journal):
    action_id = "act:" + "3" * 24
    sends = []

    def guard():
        raise Pause(Reason.USER_TAKEOVER, {"reason": "foreground changed"})

    with pytest.raises(Pause):
        journal.dispatch_once(
            action_id=action_id, request_hash="h", run_id="run:" + "a" * 24, guard=guard, send=lambda: sends.append(1)
        )
    record = journal.lookup(action_id)
    assert record is not None and record.state is DispatchState.NOT_DISPATCHED
    assert not sends


def test_uncertain_effect_is_never_replayed(journal):
    action_id = "act:" + "4" * 24
    sends = []

    def send():
        sends.append(1)
        raise UncertainEffect("partial input")

    with pytest.raises(UncertainEffect):
        journal.dispatch_once(
            action_id=action_id, request_hash="h", run_id="run:" + "a" * 24, guard=lambda: None, send=send
        )
    assert journal.lookup(action_id).state is DispatchState.UNCERTAIN
    with pytest.raises(Pause) as failure:
        journal.dispatch_once(
            action_id=action_id, request_hash="h", run_id="run:" + "a" * 24, guard=lambda: None, send=send
        )
    assert failure.value.reason_value == Reason.UNCERTAIN_EFFECT.value
    assert len(sends) == 1


def test_pre_dispatch_driver_failure_is_not_dispatched(journal):
    action_id = "act:" + "5" * 24

    def send():
        raise DriverError("geometry moved")

    with pytest.raises(DriverError):
        journal.dispatch_once(
            action_id=action_id, request_hash="h", run_id="run:" + "a" * 24, guard=lambda: None, send=send
        )
    assert journal.lookup(action_id).state is DispatchState.NOT_DISPATCHED


def test_recovery_marks_unfinished_actions_uncertain(tmp_path):
    path = str(tmp_path / "journal.sqlite")
    journal = DispatchJournal(path)
    action_id = "act:" + "6" * 24
    # Simulate a crash after intent was made durable but before the receipt was written.
    journal._db.execute(
        "INSERT INTO effects VALUES (?, ?, ?, 'dispatching', NULL, NULL, 1.0, 1.0)",
        (action_id, "h", "run:" + "a" * 24),
    )
    journal.close()

    reopened = DispatchJournal(path)
    recovered = reopened.recover_unfinished()
    assert recovered == [action_id]
    assert reopened.lookup(action_id).state is DispatchState.UNCERTAIN
    with pytest.raises(Pause):
        reopened.dispatch_once(
            action_id=action_id,
            request_hash="h",
            run_id="run:" + "a" * 24,
            guard=lambda: None,
            send=lambda: receipt(action_id),
        )
    reopened.close()


def test_reconcile_can_resolve_an_uncertain_action(journal):
    action_id = "act:" + "7" * 24
    with pytest.raises(UncertainEffect):
        journal.dispatch_once(
            action_id=action_id,
            request_hash="h",
            run_id="run:" + "a" * 24,
            guard=lambda: None,
            send=lambda: (_ for _ in ()).throw(UncertainEffect("boom")),
        )
    journal.reconcile(action_id, DispatchState.NOT_DISPATCHED, "observed state unchanged")
    assert journal.lookup(action_id).state is DispatchState.NOT_DISPATCHED
    with pytest.raises(ContractError):
        journal.reconcile(action_id, DispatchState.DISPATCHING, "nope")


def test_journal_write_failure_marks_the_store_unhealthy(tmp_path):
    """A journal that cannot durably record intent must stop the run, not silently continue."""
    journal = DispatchJournal(str(tmp_path / "journal.sqlite"))
    journal._db.close()
    with pytest.raises(JournalUnhealthy):
        journal.dispatch_once(
            action_id="act:" + "8" * 24,
            request_hash="h",
            run_id="run:" + "a" * 24,
            guard=lambda: None,
            send=lambda: receipt("act:" + "8" * 24),
        )
    assert not journal.healthy
    with pytest.raises(JournalUnhealthy):
        journal.require_healthy()


def test_closed_journal_refuses_work_instead_of_crashing(tmp_path):
    """A handler that outlives shutdown must get an error, not a dead SQLite handle."""
    journal = DispatchJournal(str(tmp_path / "journal.sqlite"))
    journal.close()
    journal.close()  # idempotent
    with pytest.raises(JournalUnhealthy):
        journal.append_trace(None, "late_write", {})
    with pytest.raises(JournalUnhealthy):
        journal.put_run(
            run_id="run:" + "d" * 24,
            spec_digest="d",
            spec_json="{}",
            status="paused",
            state_json="{}",
            resume_token=None,
        )


def test_run_records_round_trip(journal):
    run_id = "run:" + "b" * 24
    journal.put_run(
        run_id=run_id,
        spec_digest="digest",
        spec_json="{}",
        status="paused",
        state_json="{}",
        resume_token="resume:" + "c" * 24,
    )
    row = journal.get_run(run_id)
    assert row is not None and row["status"] == "paused" and row["spec_digest"] == "digest"
    journal.append_trace(run_id, "decision", {"operation": "CLICK"})
    assert journal.traces(run_id)[0]["kind"] == "decision"
