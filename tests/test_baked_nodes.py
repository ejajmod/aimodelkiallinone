import subprocess

from launcher.catalog import NodeSpec
from launcher.installer import Installer
from launcher.state import StateStore


def test_baked_node_skips_network_and_pip(tmp_path, monkeypatch) -> None:
    root = tmp_path / "ComfyUI"
    node_dir = root / "custom_nodes" / "example"
    node_dir.mkdir(parents=True)
    subprocess.run(["git", "init", str(node_dir)], check=True, capture_output=True)
    (node_dir / "requirements.txt").write_text("example-package\n", encoding="utf-8")
    (node_dir / "install.py").write_text("raise RuntimeError('must not run')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(node_dir), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(node_dir), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "test"],
        check=True,
        capture_output=True,
    )
    revision = subprocess.check_output(["git", "-C", str(node_dir), "rev-parse", "HEAD"], text=True).strip()
    (node_dir / ".aimodelki-baked-revision").write_text(revision + "\n", encoding="ascii")
    installer = Installer(root, StateStore(tmp_path / "state"), tmp_path / "restart.sh")
    monkeypatch.setattr(installer, "_run_command", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected installation")))

    installer._install_node(
        NodeSpec(
            "Example",
            "https://example.com/example.git",
            revision,
            "custom_nodes/example",
            install_script="install.py",
        )
    )
