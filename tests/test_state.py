from launcher.state import StateStore


def test_active_state_becomes_interrupted_after_restart(tmp_path) -> None:
    first = StateStore(tmp_path)
    first.update(status="downloading", workflow_id="image-generation", progress=42)

    restored = StateStore(tmp_path).get()
    assert restored["status"] == "interrupted"
    assert restored["progress"] == 42
    assert "wznowić" in restored["message"]
