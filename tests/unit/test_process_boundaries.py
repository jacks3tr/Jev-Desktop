"""Exercise OS isolation and coordinate contracts without observing or controlling a desktop."""

import multiprocessing
import struct
import time
import uuid
import zlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jev_desktop.contracts import (
    AuthorizationError,
    ContractError,
    EvidenceRef,
    Geometry,
    Pause,
    Rect,
    ScreenshotPoint,
    UncertainEffect,
)
from jev_desktop.drivers.windows import WindowsDriver
from jev_desktop.evidence import EvidenceStore, RetentionPolicy
from jev_desktop.ownership import Ownership


def test_evidence_survives_restart_and_enforces_each_run_limit(tmp_path):
    store = EvidenceStore(tmp_path, [str(tmp_path)], RetentionPolicy(max_run_bytes=4, max_total_bytes=100))
    first = store.save_bytes(
        run_id="run:" + "5" * 24,
        checkpoint=None,
        description="first",
        data=b"1111",
        kind="artifact",
        media_type="application/octet-stream",
    )
    kept = store.save_bytes(
        run_id=first.run_id,
        checkpoint=None,
        description="failure",
        data=b"2222",
        kind="artifact",
        media_type="application/octet-stream",
        keep=True,
    )
    reopened = EvidenceStore(tmp_path, [str(tmp_path)], store.retention)
    assert reopened.read(kept.evidence_id) == b"2222"
    assert reopened.prune()["removed"] == 1
    assert reopened.read(kept.evidence_id) == b"2222"
    with pytest.raises(ContractError):
        reopened.get(first.evidence_id)
    manifest = reopened.run_dir(first.run_id) / "index.jsonl"
    assert first.evidence_id not in manifest.read_text(encoding="utf-8")
    assert len(manifest.read_text(encoding="utf-8").splitlines()) == 1
    reopened.retention.max_total_bytes = 0
    assert reopened.prune()["removed"] == 1
    assert not manifest.parent.exists()
    assert EvidenceStore(tmp_path, [str(tmp_path)]).usage()["bytes"] == 0


def test_capture_encoder_makes_gdi_pixels_opaque():
    from jev_desktop.drivers.windows.capture import encode_png

    png = encode_png(1, 1, bytes((0, 0, 255, 0)))
    assert png[37:41] == b"IDAT"
    length = struct.unpack(">I", png[33:37])[0]
    assert zlib.decompress(png[41 : 41 + length]) == bytes((0, 255, 0, 0, 255))


def _lease_client(connection):
    ownership = Ownership()
    session = ownership.create_session("lease-contender")
    while connection.recv() == "acquire":
        try:
            lease = ownership.acquire(session.session_id, "run:" + "1" * 24)
        except AuthorizationError:
            connection.send(False)
        else:
            ownership.release(lease.lease_id)
            connection.send(True)


def test_lease_excludes_a_real_process_until_release():
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_lease_client, args=(child,))
    driver = WindowsDriver()
    driver._process = process
    ownership = Ownership(quiesce=driver.quiesce)
    ownership._on_acquire = driver.retain_lease
    session = ownership.create_session("lease-owner")
    try:
        process.start()
        child.close()
        lease = ownership.acquire(session.session_id, "run:" + "2" * 24)
        parent.send("acquire")
        assert parent.poll(10) and parent.recv() is False
        ownership.release(lease.lease_id)
        parent.send("acquire")
        assert parent.poll(10) and parent.recv() is True
        parent.send("exit")
        process.join(5)
        assert process.exitcode == 0
    finally:
        ownership.force_release()
        if process.is_alive():
            process.terminate()
        process.join(5)
        parent.close()


def _stalled_worker(connection, dispatch):
    connection.send(("ready", None))
    connection.recv()
    if dispatch:
        connection.send(("guard", None))
        connection.recv()
    # Real blocked process, no COM objects, app windows, or input API calls.
    connection.recv()


@pytest.mark.parametrize("dispatch", [False, True])
def test_deadline_terminates_worker_and_preserves_uncertainty(dispatch):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_stalled_worker, args=(child, dispatch))
    driver = WindowsDriver()
    driver._connection = parent
    driver._process = process
    try:
        process.start()
        child.close()
        assert parent.poll(10)
        assert parent.recv()[0] == "ready"
        driver.set_boundary(lambda: None, 0.15)
        started = time.monotonic()
        with pytest.raises(UncertainEffect if dispatch else Pause):
            driver._call("observe")
        assert not process.is_alive()
        assert time.monotonic() - started < 4
    finally:
        driver.close()


def test_coordinate_transform_binds_snapshot_crop_and_actual_image_dimensions():
    geometry = Geometry(3, -1920, 0, 3840, 1080, 144, 1.5)
    rect = Rect(-1800, 100, -799, 801)
    evidence = EvidenceRef(
        "ev:" + "1" * 24,
        "run:" + "2" * 24,
        "screenshot",
        "unused.png",
        "image/png",
        "unused",
        0,
        1.0,
        snapshot_id="snap:" + "3" * 24,
        geometry=geometry,
        source_rect=rect,
        scale=0.5,
        image_width=500,
        image_height=350,
    )
    point = ScreenshotPoint(evidence.evidence_id, 499, 349, rect, 0.5, 500, 350, 3)
    assert point.resolve(evidence, evidence.run_id, evidence.snapshot_id) == (-801, 799)
    for altered in (
        replace(point, image_width=501),
        replace(point, scale=1.0),
        replace(point, geometry_epoch=4),
        replace(point, x=500),
    ):
        with pytest.raises(ContractError):
            altered.resolve(evidence, evidence.run_id, evidence.snapshot_id)
    with pytest.raises(ContractError):
        point.resolve(evidence, "run:" + "4" * 24, evidence.snapshot_id)


def test_point_schema_error_names_the_missing_source_rect():
    point = {"evidence_id": "ev:" + "1" * 24, "x": 1, "y": 1, "crop": {"rect": Rect(0, 0, 10, 10).to_json()}}
    with pytest.raises(ContractError, match=r"^point\.source_rect must be an object$"):
        ScreenshotPoint.from_json(point)


def _returned_uncertain_worker(connection):
    connection.recv()
    connection.send(("error", ("UncertainEffect", "input outcome unknown", {"poisoned": False})))
    connection.recv()
    connection.send(("result", "fresh observation"))


def test_returned_uncertain_action_allows_fresh_observation():
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_returned_uncertain_worker, args=(child,))
    driver = WindowsDriver()
    driver._connection = parent
    driver._process = process
    try:
        process.start()
        child.close()
        with pytest.raises(UncertainEffect, match="input outcome unknown"):
            driver._call("execute", SimpleNamespace(deadline_s=5))
        assert driver._call("observe") == "fresh observation"
        process.join(5)
    finally:
        driver.close()


def test_emergency_stop_survives_its_event_dying_with_the_last_process(tmp_path, monkeypatch):
    from jev_desktop import ownership

    name = f"Local\\JevDesktopTest.{uuid.uuid4().hex}"  # never touch the real per-session stop
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(ownership, "_event_name", lambda: name)
    monkeypatch.setattr(ownership, "_event_handle", None)

    def restart() -> None:
        # Closing the only handle destroys the kernel event, as when every broker/client exits.
        ownership.kernel32.CloseHandle(ownership._event_handle)
        ownership._event_handle = None

    def markers() -> list:
        return list((tmp_path / "JevDesktop").glob("emergency-*.stop"))

    try:
        assert not ownership.emergency_is_set()
        ownership.emergency_signal()
        assert markers()
        restart()
        assert ownership.emergency_is_set(), "a restarted broker must not silently clear the stop"
        ownership.emergency_clear()
        assert not markers()
        restart()
        assert not ownership.emergency_is_set()
    finally:
        if ownership._event_handle is not None:
            ownership.kernel32.CloseHandle(ownership._event_handle)
