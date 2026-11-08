import json

from context import create_memory


def test_history_skips_non_object_rows_and_retains_valid_messages(tmp_path):
    path = tmp_path / "history.jsonl"
    rows = [[], None, "wrong", {"role": "user", "content": "hello"}, 42]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    history = create_memory(str(path))
    assert history.as_messages() == [{"role": "user", "content": "hello"}]
    history.append("assistant", "hi", user_key="a")
    assert create_memory(str(path)).as_messages()[-1] == {"role": "assistant", "content": "hi"}


def test_confirmed_turn_records_input_and_output_as_one_submission(tmp_path):
    from context.models import ConfirmedTurn

    history = create_memory(str(tmp_path / "history.jsonl"))
    receipt = history.record(ConfirmedTurn("hello", "hi", "completed", user_key="u"))
    assert receipt.input_recorded and receipt.output_recorded
    assert history.as_messages(user_key="u") == [
        {"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"},
    ]


def test_unconfirmed_output_cannot_be_submitted_as_complete_assistant(tmp_path):
    import pytest

    from context.models import ConfirmedTurn

    history = create_memory(str(tmp_path / "history.jsonl"))
    with pytest.raises(ValueError, match="确认"):
        history.record(ConfirmedTurn("question", "unplayed", "failed"))
    assert history.as_messages() == []
    receipt = history.record(ConfirmedTurn("question", "", "interrupted"))
    assert receipt.input_recorded and not receipt.output_recorded
    assert history.as_messages() == [{"role": "user", "content": "question"}]
