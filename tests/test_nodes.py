import hashlib
import io
import tarfile
from pathlib import Path

import pytest
import requests

from launcher import installer as installer_module
from launcher.catalog import NodeSpec
from launcher.installer import NODE_MARKER, InstallError, Installer
from launcher.state import StateStore


REVISION = "0123456789abcdef0123456789abcdef01234567"
REPOSITORY = "https://github.com/example/example.git"


def make_installer(tmp_path: Path) -> Installer:
    return Installer(tmp_path / "ComfyUI", StateStore(tmp_path / "state"), tmp_path / "restart.sh")


def node(**overrides) -> NodeSpec:
    values = {
        "name": "Example",
        "repository": REPOSITORY,
        "revision": REVISION,
        "directory": "custom_nodes/example",
    }
    values.update(overrides)
    return NodeSpec(**values)


def write_checkout(root: Path, head: str, marker: str | None = REVISION) -> Path:
    destination = root / "custom_nodes" / "example"
    (destination / ".git").mkdir(parents=True)
    (destination / ".git" / "HEAD").write_text(head + "\n", encoding="ascii")
    if marker:
        (destination / NODE_MARKER).write_text(marker + "\n", encoding="ascii")
    return destination


def forbid_commands(installer: Installer, monkeypatch) -> None:
    monkeypatch.setattr(installer, "_run_command", lambda *args, **kwargs: pytest.fail("unexpected command"))


def test_pinned_node_is_skipped_without_git_or_pip(tmp_path, monkeypatch) -> None:
    installer = make_installer(tmp_path)
    write_checkout(installer.comfyui_root, REVISION)
    forbid_commands(installer, monkeypatch)

    installer._install_node(node(install_script="install.py"))

    assert installer.node_ready(node())


def test_branch_checkout_or_missing_marker_is_not_ready(tmp_path) -> None:
    installer = make_installer(tmp_path)
    destination = write_checkout(installer.comfyui_root, "ref: refs/heads/main")
    assert not installer.node_ready(node())

    (destination / ".git" / "HEAD").write_text(REVISION, encoding="ascii")
    (destination / NODE_MARKER).unlink()
    assert not installer.node_ready(node())


def test_marker_from_previous_images_is_accepted(tmp_path) -> None:
    installer = make_installer(tmp_path)
    destination = write_checkout(installer.comfyui_root, REVISION, marker=None)
    (destination / ".aimodelki-baked-revision").write_text(REVISION, encoding="ascii")

    assert installer.node_ready(node())


def test_git_install_fetches_only_the_pinned_commit(tmp_path, monkeypatch) -> None:
    installer = make_installer(tmp_path)
    monkeypatch.setattr(installer_module, "NODE_CONSTRAINTS", ())
    commands: list[list[str]] = []
    pip_requirements: list[str] = []

    def run(command, cwd=None):
        commands.append(command)
        if "checkout" in command:
            (Path(command[2]) / "requirements.txt").write_text("torch\nnumpy>=1.26\n", encoding="utf-8")
        if command[1:4] == ["-m", "pip", "install"]:
            pip_requirements.append(Path(command[command.index("-r") + 1]).read_text(encoding="utf-8"))

    monkeypatch.setattr(installer, "_run_command", run)

    installer._install_node(node())

    git = [command[3:] for command in commands if command[0] == "git"]
    assert git == [
        ["init", "-q"],
        ["remote", "add", "origin", REPOSITORY],
        ["fetch", "--depth", "1", "--no-tags", REPOSITORY, REVISION],
        ["checkout", "--detach", "FETCH_HEAD"],
    ]
    assert pip_requirements == ["numpy>=1.26\n"]
    destination = installer.comfyui_root / "custom_nodes" / "example"
    assert (destination / NODE_MARKER).read_text(encoding="ascii").strip() == REVISION


def test_requirements_drop_managed_unpinned_and_option_lines(tmp_path) -> None:
    installer = make_installer(tmp_path)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "\n".join(
            [
                "# comment",
                "torch>=2",
                "onnxruntime",
                "onnxruntime-gpu",
                "TorchVision==0.25",
                "git+https://github.com/facebookresearch/sam2",
                "-r other.txt",
                "opencv-python  # video",
                "numpy>=1.26.4",
            ]
        ),
        encoding="utf-8",
    )

    filtered = installer._filtered_requirements(requirements)

    assert filtered is not None
    assert filtered.read_text(encoding="utf-8").splitlines() == ["onnxruntime-gpu", "opencv-python", "numpy>=1.26.4"]
    requirements.write_text("torch\ntorchaudio\n", encoding="utf-8")
    assert installer._filtered_requirements(requirements) is None


class FakeResponse:
    def __init__(self, payload: bytes, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_content(self, _size):
        yield self.payload


def node_archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        data = b"NODE_CLASS_MAPPINGS = {}\n"
        info = tarfile.TarInfo("__init__.py")
        info.size = len(data)
        bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_archive_install_is_verified_and_marks_the_revision(tmp_path, monkeypatch) -> None:
    payload = node_archive()
    installer = make_installer(tmp_path)
    monkeypatch.setenv("AIMODELKI_NODE_ARCHIVE_BASE_URL", "https://archives.example")
    requested: list[str] = []
    monkeypatch.setattr(
        installer_module.requests, "get", lambda url, **_kwargs: requested.append(url) or FakeResponse(payload)
    )
    forbid_commands(installer, monkeypatch)
    spec = node(archive_sha256=hashlib.sha256(payload).hexdigest())

    installer._install_node(spec)

    destination = installer.comfyui_root / "custom_nodes" / "example"
    assert requested == [f"https://archives.example/example-{REVISION}.tar.gz"]
    assert (destination / "__init__.py").is_file()
    assert installer.node_ready(spec)


def test_archive_with_wrong_checksum_falls_back_to_git(tmp_path, monkeypatch) -> None:
    installer = make_installer(tmp_path)
    monkeypatch.setenv("AIMODELKI_NODE_ARCHIVE_BASE_URL", "https://archives.example")
    monkeypatch.setattr(installer_module.requests, "get", lambda _url, **_kwargs: FakeResponse(node_archive()))
    commands: list[list[str]] = []
    monkeypatch.setattr(installer, "_run_command", lambda command, cwd=None: commands.append(command))

    installer._install_node(node(archive_sha256="f" * 64))

    destination = installer.comfyui_root / "custom_nodes" / "example"
    assert any("fetch" in command for command in commands)
    assert not (destination / "__init__.py").exists()


def test_unknown_directory_is_not_overwritten(tmp_path, monkeypatch) -> None:
    installer = make_installer(tmp_path)
    (installer.comfyui_root / "custom_nodes" / "example").mkdir(parents=True)
    forbid_commands(installer, monkeypatch)

    with pytest.raises(InstallError, match="nie jest repozytorium git"):
        installer._install_node(node())
