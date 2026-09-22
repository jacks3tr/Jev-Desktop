from __future__ import annotations

import pytest

from jev_desktop.policy import PolicyConfig


@pytest.fixture(autouse=True)
def _no_live_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests use scripted policies; a real key would make the broker call Jev."""
    monkeypatch.delenv(PolicyConfig().api_key_env, raising=False)
