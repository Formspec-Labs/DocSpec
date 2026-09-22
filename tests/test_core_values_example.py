"""Run the examples.core_values two-field revision scenario and pin its reuse, re-execution and exact reopen summary."""
from examples.core_values import run


def test_two_field_revision_reuse_and_reopen_example(tmp_path):
    summary = run(tmp_path)
    assert summary["producer_executions"] == 2
    assert summary["title_change_reused"] and summary["url_change_executed"]
    assert summary["original_execution"] == summary["retitled_execution"]
    assert summary["reopened_exact_selection"] == "retitled-request:selection"
