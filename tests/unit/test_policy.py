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
    build_contexts,
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
        contexts=[
            context(Operation.CLICK, 2),
            OpContext(
                operation=Operation.HOTKEY,
                candidates=(TargetCandidate("cfg:" + "1" * 24, "Enter in the focused window", Operation.HOTKEY),),
                note="Allowed chord: enter",
            ),
        ],
        allow_done=False,
        allow_escalate=True,
    )
    assert set(questions) == {"operation", "CLICK_target", "HOTKEY_target"}
    assert "Allowed chord: enter" in questions["operation"]["criteria"]["HOTKEY"]
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
    contexts = [context(Operation.CLICK, 1), context(Operation.TYPE_TEXT, 1)]
    questions = build_questions(goal="save", contexts=contexts, allow_done=True, allow_escalate=True)
    selected = contexts[0].candidates[0].element_id
    result = fake_response(
        "jev-1.13.0",
        {
            "operation": choice_answer("CLICK", list(questions["operation"]["criteria"])),
            "CLICK_target": choice_answer(selected, list(questions["CLICK_target"]["criteria"])),
            "TYPE_TEXT_target": {"invalid": "unused head must be ignored"},
        },
    )
    operation, target, _usage = resolve_answers(
        result,
        {"model": "jev-1.13.0", "questions": questions},
        operation_floor=0.4,
        target_floor=0.4,
        contexts=contexts,
    )
    assert operation is Operation.CLICK
    assert target is not None and target.element_id == selected


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
    waits = []
    with LocalTypeSafeServer() as server:
        server.response_headers["Retry-After"] = "2"
        server.queue(429, {"error": "slow down"})
        server.queue(200, fake_response("jev-1.13.0", body_answers))
        policy = JevPolicy(
            transport=HttpTransport(),
            config=PolicyConfig(endpoint=server.endpoint, max_retries=2),
            api_key="test-key",
            sleep=waits.append,
        )
        decision = policy.decide(goal="g", state={"elements": []}, contexts=contexts, allow_done=True)
    assert decision.operation is Operation.WAIT
    assert decision.model == "jev-1.13.0"
    assert len(server.requests) == 2, "the rate limit should have produced a second request"
    assert server.headers[0]["Authorization"] == "Bearer test-key"

    assert waits == [2.0]
    with LocalTypeSafeServer() as server:
        server.queue(429, {"error": "slow down"})
        policy = JevPolicy(
            transport=HttpTransport(),
            config=PolicyConfig(endpoint=server.endpoint, max_retries=0),
            api_key="test-key",
        )
        with pytest.raises(PolicyError, match=r"unavailable.*429"):
            policy.decide(goal="g", state={}, contexts=contexts, allow_done=True)
        assert len(server.requests) == 1


@pytest.mark.parametrize(
    ("answer", "confidence", "expected"),
    [("YES", 0.9, True), ("NO", 0.9, False), ("YES", 0.55, None)],
    ids=["visible", "absent", "unsure"],
)
def test_completion_is_confirmed_on_the_observation_without_action_history(answer, confidence, expected):
    sent = []

    class Transport:
        def post_json(self, url, *, headers, payload, timeout_s):
            sent.append(payload)
            return 200, fake_response(
                "jev-1.13.0", {"done": choice_answer(answer, ["YES", "NO"], confidence=confidence)}
            )

    policy = JevPolicy(transport=Transport(), api_key="test-key")
    state = {"elements": [], "recent_actions": [{"operation": "CLICK", "target": "Open", "changed": True}]}
    if expected is None:
        with pytest.raises(Pause) as paused:
            policy.confirm_done(goal="g", state=state)
        assert paused.value.reason is Reason.LOW_CONFIDENCE
    else:
        assert policy.confirm_done(goal="g", state=state) is expected
    assert sent[0]["state"]["recent_actions"] == []
    assert list(sent[0]["questions"]) == ["done"]


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
    trimmed = JevPolicy(
        transport=HttpTransport(),
        config=PolicyConfig(),
        api_key="  test-key\r\n",
        sleep=lambda _s: None,
    )
    assert trimmed._key() == "test-key"

    unsafe = JevPolicy(
        transport=HttpTransport(),
        config=PolicyConfig(),
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


def test_rejected_answer_retains_usage_and_provider_errors_are_sanitized():
    from types import SimpleNamespace

    response = fake_response(
        "jev-1.13.0",
        {
            "operation": {
                "type": "choice",
                "choice": "WAIT",
                "probabilities": {"WAIT": 0.34, "DONE": 0.33, "ESCALATE": 0.33},
                "confidence": 0.2,
            }
        },
        {"input_tokens": 123, "output_tokens": 45},
    )
    transport = SimpleNamespace(post_json=lambda *args, **kwargs: (200, response))
    policy = JevPolicy(transport=transport, api_key="test-key")
    with pytest.raises(Pause):
        policy.decide(goal="wait", state={}, contexts=[], allow_done=True)
    assert policy.last_attempts[0]["usage"]["input_tokens"] == 123
    assert policy.last_attempts[0]["outcome"] == "low_confidence"
    transport.post_json = lambda *args, **kwargs: (422, {"detail": "test-key"})
    with pytest.raises(PolicyError) as failure:
        policy.decide(goal="wait", state={}, contexts=[], allow_done=True)
    assert "test-key" not in str(failure.value)


def test_refused_answers_record_the_selected_option_and_its_numbers():
    def refused(attempt):
        return {key: attempt.get(key) for key in ("outcome", "selected", "confidence", "margin")}

    class Transport:
        def __init__(self, answers):
            self.answers = answers

        def post_json(self, url, *, headers, payload, timeout_s):
            return 200, fake_response("jev-1.13.0", self.answers)

    below_floor = {
        "type": "choice",
        "choice": "WAIT",
        "probabilities": {"WAIT": 0.34, "DONE": 0.33, "ESCALATE": 0.33},
        "confidence": 0.2,
    }
    policy = JevPolicy(transport=Transport({"operation": below_floor}), api_key="test-key")
    with pytest.raises(Pause):
        policy.decide(goal="g", state={}, contexts=[], allow_done=True)
    assert refused(policy.last_attempts[-1]) == {
        "outcome": "low_confidence",
        "selected": "WAIT",
        "confidence": 0.2,
        "margin": 0.01,
    }

    near_tie = {
        "type": "choice",
        "choice": "t1",
        "probabilities": {"t1": 0.46, "t2": 0.44, NONE: 0.1},
        "confidence": 0.46,
    }
    policy.transport = Transport(
        {"operation": choice_answer("CLICK", ["CLICK", "WAIT", "DONE", "ESCALATE"]), "CLICK_target": near_tie}
    )
    with pytest.raises(Pause):
        policy.decide(goal="g", state={}, contexts=[context(Operation.CLICK, 2)], allow_done=True)
    assert refused(policy.last_attempts[-1]) == {
        "outcome": "low_confidence",
        "selected": "t1",
        "confidence": 0.46,
        "margin": 0.02,
    }

    policy.transport = Transport({"done": choice_answer("YES", ["YES", "NO"], confidence=0.55)})
    with pytest.raises(Pause):
        policy.confirm_done(goal="g", state={"elements": []})
    assert refused(policy.last_attempts[-1]) == {
        "outcome": "low_confidence",
        "selected": "YES",
        "confidence": 0.55,
        "margin": 0.1,
    }


def test_provider_receives_full_context_without_local_byte_caps():
    from dataclasses import replace

    from jev_desktop.contracts import canonical_json

    contexts = [
        replace(
            group, candidates=tuple(replace(c, description=c.description + " context" * 40) for c in group.candidates)
        )
        for group in [context(Operation.CLICK, 30), context(Operation.TOGGLE, 30), context(Operation.SELECT, 30)]
    ]
    questions = build_questions(goal="edit", contexts=contexts, allow_done=True, allow_escalate=True)
    answer = choice_answer("WAIT", list(questions["operation"]["criteria"]))
    with LocalTypeSafeServer() as server:
        server.queue(200, fake_response("jev-1.13.0", {"operation": answer}))
        policy = JevPolicy(transport=HttpTransport(), config=PolicyConfig(endpoint=server.endpoint), api_key="test-key")
        state = {"reference": "long context " * 6000}
        assert policy.decide(goal="edit", state=state, contexts=contexts, allow_done=True).operation is Operation.WAIT
    body = server.requests[0]
    assert len(canonical_json(body).encode()) > 60000
    assert body["state"] == state


def test_choice_accepts_rounded_distribution_without_lowering_confidence_floor():
    answer = {"type": "choice", "choice": "a", "probabilities": {"a": 0.77, "b": 0.11, "c": 0.11}, "confidence": 0.75}
    assert valid_choice(answer, {"a", "b", "c"}, 0.45) == "a"
    with pytest.raises(Pause) as failure:
        valid_choice(answer, {"a", "b", "c"}, 0.8)
    assert failure.value.reason_value == "low_confidence"


def test_near_tie_between_targets_pauses_even_above_the_confidence_floor():
    options = [f"t{index}" for index in range(1, 150)] + [NONE]
    tail = (1.0 - 0.46 - 0.44) / (len(options) - 2)
    probabilities = dict.fromkeys(options, tail) | {"t1": 0.46, "t2": 0.44}
    answer = {"type": "choice", "choice": "t1", "probabilities": probabilities, "confidence": 0.46}
    assert valid_choice(answer, set(options), 0.45) == "t1"
    with pytest.raises(Pause) as failure:
        valid_choice(answer, set(options), 0.45, 0.1)
    assert failure.value.reason_value == Reason.LOW_CONFIDENCE.value


def test_diffuse_rounded_distribution_is_low_confidence_not_malformed():
    # 200 options: the tail rounds to zero, so the reported sum falls well short of 1.
    options = {f"t{index}" for index in range(200)}
    probabilities = dict.fromkeys(options, 0.0) | {"t0": 0.3, "t1": 0.25, "t2": 0.2}
    answer = {"type": "choice", "choice": "t0", "probabilities": probabilities, "confidence": 0.2}
    with pytest.raises(Pause) as failure:
        valid_choice(answer, options, 0.45)
    assert failure.value.reason_value == Reason.LOW_CONFIDENCE.value


def test_target_margin_must_be_a_probability():
    with pytest.raises(PolicyError):
        PolicyConfig(target_margin=1.5).validate()


def test_wire_targets_resolve_to_original_control_without_truncating_text():
    from jev_desktop.policy import decision_state

    target = context(Operation.CLICK, 1)
    native_id = target.candidates[0].element_id
    state = {
        "elements": [
            {
                "element_id": native_id,
                "index": 1,
                "role": "button",
                "name": "Save",
                "text": "evidence " * 1000,
                "focused": True,
            }
        ]
    }
    with LocalTypeSafeServer() as server:
        server.queue(
            200,
            fake_response(
                "jev-1.13.0",
                {
                    "operation": choice_answer("CLICK", ["CLICK", "WAIT", "DONE", "ESCALATE"]),
                    "CLICK_target": choice_answer("t1", ["t1", "NONE"]),
                },
            ),
        )
        policy = JevPolicy(transport=HttpTransport(), config=PolicyConfig(endpoint=server.endpoint), api_key="test-key")
        result = policy.decide(goal="Save", state=state, contexts=[target], allow_done=True)
    assert result.target == target.candidates[0]
    sent = server.requests[0]["state"]["elements"][0]
    assert sent["id"] == "t1" and sent["text"] == state["elements"][0]["text"]
    assert sent["focused"] is True
    assert len(decision_state(state, {})["elements"]) == 1


def test_unavailable_controls_are_not_offered_as_action_candidates():
    contexts = build_contexts(
        observation={
            "elements": [
                {"element_id": "enabled", "operations": ["CLICK"], "enabled": True, "visible": True},
                {"element_id": "disabled", "operations": ["CLICK"], "enabled": False, "visible": True},
                {"element_id": "hidden", "operations": ["CLICK"], "enabled": True, "visible": False},
            ]
        },
        operations=[Operation.CLICK],
    )
    assert [candidate.element_id for candidate in contexts[0].candidates] == ["enabled"]
