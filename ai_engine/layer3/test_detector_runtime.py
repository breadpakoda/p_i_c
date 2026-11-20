from types import SimpleNamespace
from unittest.mock import Mock
import json

import pytest

from ai_engine.layer3 import detector


@pytest.mark.parametrize(("response", "expected"), [("YES", True), ("NO", False)])
def test_layer3_parses_exact_llm_verdict(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    response: str,
    expected: bool,
) -> None:
    invoke = Mock(return_value=SimpleNamespace(content=response))
    monkeypatch.setattr(detector, "llm", SimpleNamespace(invoke=invoke))

    conversation = [
        {"role": "user", "content": "What is the capital of France?"},
        {"role": "assistant", "content": "Paris."},
    ]
    result = detector.is_danger_l3(conversation)

    assert result is expected
    invoke.assert_called_once()
    output = capsys.readouterr().out
    assert "[Layer 3] Received conversation:" in output
    assert "What is the capital of France?" in output
    sent_history = json.loads(invoke.call_args.args[0][1].content)
    assert sent_history == conversation


def test_layer3_rejects_non_binary_llm_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoke = Mock(return_value=SimpleNamespace(content="Probably safe"))
    monkeypatch.setattr(detector, "llm", SimpleNamespace(invoke=invoke))

    with pytest.raises(ValueError, match="Unexpected LLM response"):
        detector.is_danger_l3([{"role": "user", "content": "hello"}])


@pytest.mark.parametrize("conversation", [[], "not a list", [{"role": "user"}]])
def test_invalid_conversation_short_circuits_without_llm(
    monkeypatch: pytest.MonkeyPatch, conversation: object
) -> None:
    invoke = Mock()
    monkeypatch.setattr(detector, "llm", SimpleNamespace(invoke=invoke))

    assert detector.is_danger_l3(conversation) is False
    invoke.assert_not_called()
