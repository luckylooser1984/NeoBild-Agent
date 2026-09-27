"""Tests for llm_router -- pure logic, no live calls (fake clients)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import router  # noqa: E402


# ---------------------------------------------------------------------------
# classify() -- classification logic
# ---------------------------------------------------------------------------

def test_code_keyword_routes_to_coder():
    d = router.classify("Write a Python script that computes Fibonacci")
    assert d.model == "coder"
    assert d.tier == 2
    assert d.local is True


def test_summarize_keyword_routes_to_qwen():
    d = router.classify("Summarize the following text: ...")
    assert d.model == "qwen"
    assert d.tier == 1
    assert d.local is True


def test_default_routes_to_qwen():
    d = router.classify("How would you generally assess the weather today?")
    assert d.model == "qwen"
    assert d.local is True


def test_explicit_task_type_overrides_keywords():
    # Sounds like a summary, but --task code forces coder.
    d = router.classify("Summarize this", task_type="code")
    assert d.model == "coder"


def test_long_prompt_is_complex_and_routes_cloud_with_key():
    long_task = "x" * 3001
    d = router.classify(long_task, api_key_present=True)
    assert d.complex_ is True
    assert d.target == "cloud"
    assert d.model == router.CLOUD_MODEL
    assert d.local is False


def test_complex_without_key_falls_back_local():
    long_task = "x" * 3001
    d = router.classify(long_task, api_key_present=False)
    assert d.target == "local"
    assert d.local is True


def test_private_source_forces_local_even_when_complex_and_key_present():
    long_task = "x" * 3001
    d = router.classify(
        long_task,
        source="notes/private/draft.md",
        cloud_flag=True,
        api_key_present=True,
    )
    assert d.private is True
    assert d.target == "local"
    assert d.local is True


def test_private_marker_confidential():
    d = router.classify("x", source=".../confidential/report.md")
    assert d.private is True
    assert d.local is True


def test_private_marker_embargoed():
    d = router.classify("x", source="/some/path/embargoed/notes.md")
    assert d.private is True


def test_explicit_cloud_flag_routes_cloud_when_key_present():
    d = router.classify("short task", cloud_flag=True, api_key_present=True)
    assert d.target == "cloud"
    assert d.reason.startswith("explicit --cloud")


def test_unknown_task_type_raises():
    try:
        router.classify("x", task_type="nonsense")
        assert False, "should raise RouterError"
    except router.RouterError:
        pass


# ---------------------------------------------------------------------------
# run() with fake clients -- orchestration without a real network
# ---------------------------------------------------------------------------

def fake_local(model, messages):
    return {"choices": [{"message": {"content": f"local answer from {model}"}}]}, 42


def fake_cloud(messages, api_key):
    assert api_key == "dummy-key"
    return {"choices": [{"message": {"content": "cloud answer"}}]}, 99


def test_run_dispatches_to_local_fake(monkeypatch):
    monkeypatch.delenv(router.CLOUD_KEY_ENV, raising=False)
    content, meta = router.run(
        "Write a bash script", "auto", "", False,
        call_local=fake_local, call_cloud_fn=fake_cloud,
    )
    assert "coder" in content
    assert meta["model"] == "coder"
    assert meta["local"] is True
    assert meta["duration_ms"] == 42


def test_run_dispatches_to_cloud_fake(monkeypatch):
    monkeypatch.setenv(router.CLOUD_KEY_ENV, "dummy-key")
    monkeypatch.setattr(router, "CLOUD_URL", "https://cloud.example/v1/chat/completions")
    monkeypatch.setattr(router, "CLOUD_MODEL", "example-model")
    long_task = "y" * 3001
    content, meta = router.run(
        long_task, "auto", "", False,
        call_local=fake_local, call_cloud_fn=fake_cloud,
    )
    assert content == "cloud answer"
    assert meta["model"] == router.CLOUD_MODEL
    assert meta["local"] is False


def test_run_private_source_never_dispatches_to_cloud_even_with_key(monkeypatch):
    monkeypatch.setenv(router.CLOUD_KEY_ENV, "dummy-key")
    long_task = "y" * 3001
    content, meta = router.run(
        long_task, "auto",
        "notes/private/draft.md",
        cloud_flag=True,
        call_local=fake_local, call_cloud_fn=fake_cloud,
    )
    assert meta["local"] is True
    assert meta["private"] is True
    assert "local answer" in content


def test_run_key_without_cloud_config_stays_local(monkeypatch):
    monkeypatch.setenv(router.CLOUD_KEY_ENV, "dummy-key")
    monkeypatch.setattr(router, "CLOUD_URL", "")
    monkeypatch.setattr(router, "CLOUD_MODEL", "")
    content, meta = router.run(
        "short task", "auto", "", cloud_flag=True,
        call_local=fake_local, call_cloud_fn=fake_cloud,
    )
    assert content != "cloud answer"
    assert meta["local"] is True
