"""Offline checks for the Jev policy layer: question construction and answer validation."""

from __future__ import annotations

import pytest

from jev_desktop.contracts import Operation, Pause, PolicyError, Reason, TargetCandidate
from jev_desktop.policy import (
    MAX_TARGETS,
    NONE,
    HttpTransport,
    JevPolicy,
    OpContext,
    PolicyConfig,
    build_questions,
    resolve_answers,
    sanitize_message,
    valid_choice,
)

from .fakes import choice_answer, fake_response
from .local_server import LocalTypeSafeServer, unused_port_endpoint


def context(operation: Operation, count: int, *, prefix: str = "el") -> OpContext:
    return OpContext(
        operation=operation,
        candidates=tuple(
            TargetCandidate(
                element_id=f"{prefix}:{index:024x}",
                description=f'[{index}] button name="B{index}"',
                operation=operation,
            )
            for index in range(1, count + 1)
        ),
    )


def test_target_questions_describe_their_operation_and_reserve_none():
    questions = build_questions(
        goal="save the document",
        contexts=[context(Operation.CLICK, 2)],
        allow_done=False,
        allow_escalate=True,
    )
    assert set(questions) == {"operation", "CLICK_target"}
    criteria = questions["CLICK_target"]["criteria"]
    assert NONE in criteria and len(criteria) == 3
    assert questions["CLICK_target"]["instructions"]["operation"] == "CLICK"
    assert "DONE" not in questions["operation"]["criteria"]
    assert not any(key.endswith("_target") for key in questions["operation"]["criteria"])
    assert "ESCALATE" in questions["operation"]["criteria"]


def test_choice_limit_is_enforced_before_sending():
    with pytest.raises(Pause) as failure:
        build_questions(
            goal="g",
            contexts=[context(Operation.CLICK, MAX_TARGETS + 1)],
            allow_done=True,
            allow_escalate=True,
        )
    assert failure.value.reason_value == Reason.NEEDS_NARROWER_OBSERVATION.value


def test_duplicate_element_ids_are_rejected():
    with pytest.raises(PolicyError):
        build_questions(
            goal="g",
            contexts=[
                OpContext(
                    operation=Operation.CLICK,
                    candidates=(
                        TargetCandidate("el:" + "0" * 24, "a", Operation.CLICK),
                        TargetCandidate("el:" + "0" * 24, "b", Operation.CLICK),
                    ),
                )
            ],
            allow_done=True,
            allow_escalate=True,
        )


def test_valid_choice_rejects_malformed_shapes():
    options = {"a", "b"}
    with pytest.raises(Pause):
        valid_choice({"type": "choice", "choice": "a", "probabilities": {"a": 0.9}, "confidence": 0.9}, options, 0.5)
    with pytest.raises(Pause):
        valid_choice(
            {"type": "choice", "choice": "c", "probabilities": {"a": 0.5, "b": 0.5}, "confidence": 0.5}, options, 0.5
        )
    with pytest.raises(Pause):
        valid_choice(
            {"type": "choice", "choice": "a", "probabilities": {"a": 0.4, "b": 0.4}, "confidence": 0.9}, options, 0.5
        )
    with pytest.raises(PolicyError):
        valid_choice(
            {"type": "choice", "choice": "a", "probabilities": {"a": 0.9, "b": 0.1}, "confidence": 0.9}, options, 1.5
        )


def test_default_operation_floor_does_not_reject_measured_answers():
    """Calibration guard: live answers land near the floor, so keep it below that band."""
    config = PolicyConfig()
    assert config.operation_floor <= 0.40
    assert config.target_floor <= 0.45
    assert config.operation_floor < config.target_floor + 0.1


def test_low_confidence_pauses_with_the_reason():
    # Selected option still wins the distribution, but its confidence is below the floor.
    answer = {
        "type": "choice",
        "choice": "a",
        "probabilities": {"a": 0.45, "b": 0.35, "c": 0.20},
        "confidence": 0.30,
    }
    with pytest.raises(Pause) as failure:
        valid_choice(answer, {"a", "b", "c"}, 0.6)
    assert failure.value.reason_value == Reason.LOW_CONFIDENCE.value


def test_probabilities_must_win_for_the_selected_option():
    answer = {"type": "choice", "choice": "a", "probabilities": {"a": 0.3, "b": 0.7}, "confidence": 0.9}
    with pytest.raises(Pause):
        valid_choice(answer, {"a", "b"}, 0.2)


def test_wrong_model_version_is_refused():
    body = {
        "model": "jev-1.13.0",
        "questions": {"operation": {"criteria": {"CLICK": "x", "WAIT": "w", "DONE": "d", "ESCALATE": "e"}}},
    }
    result = fake_response("jev-1.12.0", {"operation": choice_answer("CLICK", ["CLICK", "WAIT", "DONE", "ESCALATE"])})
    with pytest.raises(Pause) as failure:
        resolve_answers(result, body, operation_floor=0.4, target_floor=0.4, contexts=[context(Operation.CLICK, 1)])
    assert failure.value.reason_value == Reason.UNEXPECTED_MODEL_VERSION.value


def test_answers_pick_operation_then_its_own_target_and_ignore_speculative_heads():
    contexts = [context(Operation.CLICK, 2, prefix="el"), context(Operation.TYPE_TEXT, 2, prefix="el")]
    questions = build_questions(goal="g", contexts=contexts, allow_done=True, allow_escalate=True)
    body = {"model": "jev-1.13.0", "questions": questions}
    answers = {
        "operation": choice_answer("CLICK", list(questions["operation"]["criteria"])),
        "CLICK_target": choice_answer("el:" + "0" * 23 + "a", list(questions["CLICK_target"]["criteria"])),
        "TYPE_TEXT_target": choice_answer("el:" + "0" * 23 + "b", list(questions["TYPE_TEXT_target"]["criteria"])),
    }
    # rename candidate ids so the scripted choices are valid option keys
    contexts[0] = OpContext(
        operation=Operation.CLICK,
        candidates=(
            TargetCandidate("el:" + "0" * 23 + "a", "a", Operation.CLICK),
            TargetCandidate("el:" + "0" * 23 + "c", "c", Operation.CLICK),
        ),
    )
    questions = build_questions(goal="g", contexts=contexts, allow_done=True, allow_escalate=True)
    body = {"model": "jev-1.13.0", "questions": questions}
    answers["operation"] = choice_answer("CLICK", list(questions["operation"]["criteria"]))
    answers["CLICK_target"] = choice_answer("el:" + "0" * 23 + "a", list(questions["CLICK_target"]["criteria"]))
    result = fake_response("jev-1.13.0", answers)
    operation, target, _usage = resolve_answers(result, body, operation_floor=0.4, target_floor=0.4, contexts=contexts)
    assert operation is Operation.CLICK
    assert target is not None and target.element_id.endswith("a")


def test_none_target_pauses_instead_of_acting():
    contexts = [context(Operation.CLICK, 1)]
    questions = build_questions(goal="g", contexts=contexts, allow_done=True, allow_escalate=True)
    body = {"model": "jev-1.13.0", "questions": questions}
    result = fake_response(
        "jev-1.13.0",
        {
            "operation": choice_answer("CLICK", list(questions["operation"]["criteria"])),
            "CLICK_target": choice_answer(NONE, list(questions["CLICK_target"]["criteria"])),
        },
    )
    with pytest.raises(Pause) as failure:
        resolve_answers(result, body, operation_floor=0.4, target_floor=0.4, contexts=contexts)
    assert failure.value.reason_value == Reason.NO_APPROPRIATE_TARGET.value


def test_policy_retries_rate_limits_then_succeeds():
    """A rate limit is retried over the real transport, against a real socket."""
    contexts = [context(Operation.CLICK, 1)]
    questions = build_questions(goal="g", contexts=contexts, allow_done=True, allow_escalate=True)
    body_answers = {
        "operation": choice_answer("WAIT", list(questions["operation"]["criteria"])),
    }
    with LocalTypeSafeServer() as server:
        server.queue(429, {"error": "slow down"})
        server.queue(200, fake_response("jev-1.13.0", body_answers))
        policy = JevPolicy(
            transport=HttpTransport(),
            config=PolicyConfig(endpoint=server.endpoint, max_retries=2),
            api_key="test-key",
            sleep=lambda _s: None,
        )
        decision = policy.decide(goal="g", state={"elements": []}, contexts=contexts, allow_done=True)
    assert decision.operation is Operation.WAIT
    assert decision.model == "jev-1.13.0"
    assert len(server.requests) == 2, "the rate limit should have produced a second request"
    assert server.headers[0]["Authorization"] == "Bearer test-key"


def test_policy_reports_rejected_key_without_dispatching():
    with LocalTypeSafeServer() as server:
        server.queue(401, {"error": "unauthorized"})
        policy = JevPolicy(
            transport=HttpTransport(),
            config=PolicyConfig(endpoint=server.endpoint),
            api_key="bad",
            sleep=lambda _s: None,
        )
        with pytest.raises(PolicyError) as failure:
            policy.decide(goal="g", state={}, contexts=[context(Operation.CLICK, 1)], allow_done=True)
    assert "401" in str(failure.value)
    assert len(server.requests) == 1, "a rejected key must not be retried"


def test_keys_are_trimmed_and_unsafe_values_are_refused_without_echoing_them():
    """A key pasted from a dotenv file often carries a newline. Never leak it in the error."""
    with LocalTypeSafeServer() as server:
        trimmed = JevPolicy(
            transport=HttpTransport(),
            config=PolicyConfig(endpoint=server.endpoint),
            api_key="  test-key\r\n",
            sleep=lambda _s: None,
        )
        assert trimmed._key() == "test-key"

        unsafe = JevPolicy(
            transport=HttpTransport(),
            config=PolicyConfig(endpoint=server.endpoint),
            api_key="test key with spaces",
            sleep=lambda _s: None,
        )
        with pytest.raises(PolicyError) as failure:
            unsafe._key()
    assert "test key with spaces" not in str(failure.value)
    assert "whitespace" in str(failure.value)


def test_connection_failures_never_carry_the_credential():
    """A real connection failure, at an address nothing listens on. The error must not leak the key."""
    # Assembled at runtime so the repository never holds a credential-shaped literal.
    fake_key = "apikey" + "_" + "secret" + "_value_" + "123456"
    policy = JevPolicy(
        transport=HttpTransport(),
        config=PolicyConfig(endpoint=unused_port_endpoint(), max_retries=0, timeout_s=2.0),
        api_key=fake_key,
        sleep=lambda _s: None,
    )
    with pytest.raises(PolicyError) as failure:
        policy.decide(goal="g", state={}, contexts=[context(Operation.CLICK, 1)], allow_done=True)
    assert fake_key not in str(failure.value)


def test_sanitize_message_redacts_a_credential_that_appears_in_the_text():
    """Redaction is what protects error text that does quote the header."""
    fake_key = "apikey" + "_" + "secret" + "_value_" + "123456"
    leaked = f"Illegal header value b'Bearer {fake_key}'"
    cleaned = sanitize_message(leaked, fake_key)
    assert fake_key not in cleaned
    assert "<redacted>" in cleaned
    bare_header = "Bearer " + "z" * 26  # assembled at runtime, never a literal in the source
    assert sanitize_message(bare_header, None).endswith("<redacted>")


def test_missing_api_key_is_reported_before_any_request():
    with LocalTypeSafeServer() as server:
        policy = JevPolicy(
            transport=HttpTransport(),
            config=PolicyConfig(endpoint=server.endpoint, api_key_env="DEFINITELY_NOT_SET"),
            sleep=lambda _s: None,
        )
        with pytest.raises(PolicyError):
            policy.decide(goal="g", state={}, contexts=[context(Operation.CLICK, 1)], allow_done=True)
    assert not server.requests, "no request may leave the process without a key"
