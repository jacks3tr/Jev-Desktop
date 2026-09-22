"""Exercise the real CLI and Windows discovery against an isolated broker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    from jev_desktop.broker import Broker, BrokerConfig
    from jev_desktop.ipc import ConnectionClosed, PipeClient, PipeServer

    run_id = uuid.uuid4().hex
    output = ROOT / ".artifacts" / "verification" / run_id
    output.mkdir(parents=True)
    pipe = rf"\\.\pipe\jev-verify-{run_id}"
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8")
    environment.pop("TYPESAFE_API_KEY", None)
    transcript = []

    def cli(*args: str) -> dict:
        command = [
            sys.executable,
            "-m",
            "jev_desktop.transports.cli",
            "--pipe",
            pipe,
            "--no-autostart",
            "--timeout",
            "20",
            *args,
        ]
        result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, encoding="utf-8", timeout=30)
        transcript.append(
            {"command": command, "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        )
        (output / "transcript.json").write_text(json.dumps(transcript, indent=2), encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"CLI failed; see {output}")
        return json.loads(result.stdout)

    try:
        with tempfile.TemporaryDirectory(prefix="jev-verify-") as scratch:
            broker = Broker(BrokerConfig(home=Path(scratch), evidence_dir=output / "evidence"))
            server = PipeServer(name=pipe, handler=broker.handle, on_disconnect=broker.on_disconnect)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            try:
                broker.start()
                thread.start()
                doctor = cli("doctor")
                health = doctor["broker"]
                assert health["reachable"] and health["health"]["pid"] == os.getpid(), doctor
                assert health["health"]["journal"]["healthy"], doctor
                assert health["capabilities"]["transport"] == "named_pipe", doctor
                (output / "doctor.json").write_text(json.dumps(doctor, indent=2), encoding="utf-8")
                listing = cli("inspect", "--no-screenshot")
                assert isinstance(listing["applications"], list), listing
                assert listing["windows"], "No interactive windows discovered"
                refs = {app["app_ref"] for app in listing["applications"]}
                assert all(window["app_ref"] in refs for window in listing["windows"]), listing
                missing = cli("inspect", "--query", f"jev-no-match-{run_id}", "--no-screenshot")
                assert missing["applications"] == [] and missing["windows"] == [], missing
            finally:
                server.stop()
                if thread.ident is not None:
                    wake = PipeClient(name=pipe, timeout_s=1)
                    try:
                        wake.connect(timeout_s=1)
                    except ConnectionClosed:
                        pass
                    finally:
                        wake.close()
                    thread.join(timeout=10)
                broker.close()
                assert not thread.is_alive(), "Pipe server did not stop"
        (output / "cleanup.json").write_text(
            json.dumps({"server_stopped": True, "scratch_removed": not Path(scratch).exists()}), encoding="utf-8"
        )
        assert (output / "transcript.json").stat().st_size > 0
        assert not Path(scratch).exists()
        print(f"PASS: discovery, filtering, doctor, cleanup. Evidence: {output}")
    except BaseException:
        print(f"FAILED: retained evidence: {output}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
