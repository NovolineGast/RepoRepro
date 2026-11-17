"""Docker lifecycle, fail-closed settings, real bounded pipe handling and evidence."""

import ast
import io
import json
import sys
import tarfile
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from git import Repo

from repo_repro.core.exceptions import PolicyViolation, RepoReproError
from repo_repro.core.models import CommandSpec, SandboxPolicy, Termination
from repo_repro.sandbox.docker_runner import (
    _BOOTSTRAP,
    _PROBE,
    DockerSandboxRunner,
    SandboxCleanupError,
)
from repo_repro.sandbox.metrics import memory_bytes, stats_sample
from repo_repro.sandbox.process import ProcessOutput, capture_process
from repo_repro.sandbox.snapshot import snapshot_repository
from repo_repro.storage.artifacts import FileArtifactStore

IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def policy() -> SandboxPolicy:
    return SandboxPolicy(isolation="reviewed_docker", timeout_seconds=2)


@pytest.fixture
def owned_repository(tmp_path: Path) -> Path:
    path = tmp_path / "source"
    path.mkdir()
    (path / "main.py").write_bytes(b"print('fixture only')\n")
    (path / ".env").write_text("TOKEN=must-not-copy", encoding="utf-8")
    with Repo.init(path, initial_branch="main") as repo:
        repo.index.add(["main.py", ".env"])
        repo.index.commit("self-owned fixture", skip_hooks=True)
    return path


class FakeDocker:
    def __init__(self, policy: SandboxPolicy) -> None:
        self.policy = policy
        self.calls: list[list[str]] = []
        self.exists = False
        self.running = False
        self.exit_code = 0
        self.fail_create = False
        self.fail_cleanup = False
        self.fail_attach = False
        self.fail_kill = False
        self.capabilities: dict[str, object] = {}
        self.host_overrides: dict[str, object] = {}
        self.probe_overrides: dict[str, object] = {}
        self.image_overrides: dict[str, object] = {}
        self.foreign_owner = False
        self.name = ""
        self.outcome = ProcessOutput(0, b"fixture stdout\n", b"fixture stderr\n")

    def run(self, arguments: Sequence[str], *, timeout: float = 15) -> ProcessOutput:
        arguments = list(arguments)
        self.calls.append(arguments)
        result: object
        if arguments[0] == "info":
            result = {
                "OSType": "linux",
                "CgroupVersion": "2",
                "MemoryLimit": True,
                "SwapLimit": True,
                "CpuCfsQuota": True,
                "PidsLimit": True,
            } | self.capabilities
        elif arguments[:2] == ["image", "inspect"]:
            result = [
                {
                    "Id": IMAGE,
                    "Os": "linux",
                    "Architecture": "amd64",
                    "Config": {} | self.image_overrides,
                }
            ]
        elif arguments[0] == "create":
            self.exists = True
            self.name = arguments[arguments.index("--name") + 1]
            return ProcessOutput(1 if self.fail_create else 0, b"container-id", b"")
        elif arguments[0] == "inspect":
            if not self.exists:
                return ProcessOutput(1, b"", b"Error: No such object")
            host = {
                "Privileged": False,
                "ReadonlyRootfs": True,
                "NetworkMode": "none",
                "Memory": self.policy.memory_bytes,
                "MemorySwap": self.policy.memory_bytes,
                "NanoCpus": round(self.policy.cpus * 1_000_000_000),
                "PidsLimit": self.policy.pids,
                "IpcMode": "private",
                "PidMode": "",
                "CgroupnsMode": "private",
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges=true"],
            } | self.host_overrides
            result = [
                {
                    "Config": {
                        "User": "10001:10001",
                        "Tty": False,
                        "Labels": {
                            "repo-repro.managed": "false" if self.foreign_owner else "true",
                            "repo-repro.execution": self.name.removeprefix("repo-repro-"),
                        },
                    },
                    "HostConfig": host,
                    "Mounts": [],
                    "State": {
                        "Running": self.running,
                        "ExitCode": self.exit_code,
                        "OOMKilled": False,
                        "Error": "",
                    },
                }
            ]
        elif arguments[0] == "exec":
            result = {
                "memory": str(self.policy.memory_bytes),
                "swap": "0",
                "pids": str(self.policy.pids),
                "cpu": f"{int(self.policy.cpus * 100000)} 100000",
                "uid": 10001,
                "capabilities": "0000000000000000",
                "no_new_privileges": "1",
                "seccomp": "2",
                "workspace_bytes": self.policy.workspace_bytes_limit,
                "python_version": "3.11.0 fixture",
                "packages": [["pip", "25.0"]],
            } | self.probe_overrides
        elif arguments[0] == "stats":
            result = {"MemUsage": "8MiB / 2GiB", "CPUPerc": "0.1%", "PIDs": "2"}
        elif arguments[0] == "kill":
            if self.fail_kill:
                return ProcessOutput(1, b"", b"kill failed")
            self.running = False
            self.exit_code = 137
            return ProcessOutput(0, b"", b"")
        elif arguments[0] == "rm":
            if self.fail_cleanup:
                return ProcessOutput(1, b"", b"remove failed")
            self.exists = self.running = False
            return ProcessOutput(0, b"", b"")
        else:
            raise AssertionError(f"unexpected Docker call: {arguments[0]}")
        return ProcessOutput(0, json.dumps(result).encode(), b"")

    def attach(
        self,
        name: str,
        payload: bytes,
        *,
        timeout: float,
        output_limit: int,
        before_input: Callable[[], None],
        on_abort: Callable[[], None],
    ) -> ProcessOutput:
        self.calls.append(["attach", name])
        self.running = True
        before_input()
        if self.fail_attach:
            raise OSError("lost daemon connection")
        if self.outcome.timed_out or self.outcome.output_limited or self.outcome.cancelled:
            on_abort()
        else:
            self.running = False
        return self.outcome


def runner(tmp_path: Path, fake: FakeDocker) -> DockerSandboxRunner:
    return DockerSandboxRunner(
        image=IMAGE,
        artifacts=FileArtifactStore(tmp_path / "evidence"),
        transport=fake,
    )


@pytest.mark.parametrize("failure", ["exit", "timeout", "output_limit"])
def test_unavailable_daemon_gives_safe_hints_without_creating_a_container(
    tmp_path: Path,
    owned_repository: Path,
    policy: SandboxPolicy,
    failure: str,
) -> None:
    class UnavailableDocker(FakeDocker):
        def run(self, arguments: Sequence[str], *, timeout: float = 15) -> ProcessOutput:
            self.calls.append(list(arguments))
            return ProcessOutput(
                1 if failure == "exit" else 0,
                b"",
                b"Authorization: Bearer synthetic-private-daemon-detail",
                timed_out=failure == "timeout",
                output_limited=failure == "output_limit",
            )

    fake = UnavailableDocker(policy)
    backend = runner(tmp_path, fake)
    result = backend.run(
        owned_repository, CommandSpec(argv=("python", "main.py")), resource_limits=policy
    )
    assert result.termination is Termination.INFRA_ERROR
    assert result.exit_code is None
    assert fake.calls == [["info", "--format", "{{json .}}"]]
    assert not fake.exists and not fake.running
    stdout, stderr = backend.read_output(result)
    assert stdout == ""
    assert "docker version" in stderr and "docker context show" in stderr
    assert "Linux" in stderr and "daemon" in stderr
    assert "synthetic-private-daemon-detail" not in stderr
    snapshot = json.loads(backend.artifacts.read_bytes(result.backend_snapshot))
    assert snapshot["cleanup_confirmed"] is True
    assert "docker context show" in snapshot["infrastructure_error"]
    assert "synthetic-private-daemon-detail" not in json.dumps(snapshot)
    assert backend.artifacts.read_bytes(result.revision.source)


def test_success_records_actual_exit_logs_snapshot_and_confirmed_cleanup(
    tmp_path: Path,
    owned_repository: Path,
    policy: SandboxPolicy,
) -> None:
    fake = FakeDocker(policy)
    backend = runner(tmp_path, fake)
    result = backend.run(
        owned_repository, CommandSpec(argv=("python", "main.py")), resource_limits=policy
    )
    assert result.exit_code == 0
    assert result.termination is Termination.EXITED
    assert backend.read_output(result) == ("fixture stdout\n", "fixture stderr\n")
    assert result.resource_usage is not None and result.resource_usage.cgroup_verified
    assert result.started_at <= result.finished_at
    assert not fake.exists and not fake.running
    snapshot: dict[str, object] = json.loads(backend.artifacts.read_bytes(result.backend_snapshot))
    assert snapshot["cleanup_confirmed"] is True
    assert snapshot["excluded_source_paths"] == [".env"]


def test_container_arguments_never_mount_host_or_forward_secrets(
    tmp_path: Path,
    owned_repository: Path,
    policy: SandboxPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-forward")
    fake = FakeDocker(policy)
    backend = runner(tmp_path, fake)
    backend.run(owned_repository, CommandSpec(argv=("python", "main.py")), resource_limits=policy)
    created = next(call for call in fake.calls if call[0] == "create")
    assert "--user=10001:10001" in created
    assert "--network=none" in created
    assert "--read-only" in created
    assert "--cap-drop=ALL" in created
    assert f"--memory={policy.memory_bytes}" in created
    assert f"--memory-swap={policy.memory_bytes}" in created
    assert f"--pids-limit={policy.pids}" in created
    assert "--privileged" not in created
    assert "--mount" not in created and "--volume" not in created
    assert not any("docker.sock" in arg or "must-not-forward" in arg for arg in created)
    assert not any(call[0] == "build" for call in fake.calls)


@pytest.mark.parametrize("failure", ["timed_out", "output_limited", "cancelled"])
def test_aborted_execution_kills_the_container_and_removes_it(
    tmp_path: Path,
    owned_repository: Path,
    policy: SandboxPolicy,
    failure: str,
) -> None:
    fake = FakeDocker(policy)
    fake.outcome = ProcessOutput(137, b"partial", b"", **{failure: True})
    result = runner(tmp_path, fake).run(
        owned_repository,
        CommandSpec(argv=("python", "main.py")),
        resource_limits=policy,
    )
    assert (
        result.termination
        is {
            "timed_out": Termination.TIMEOUT,
            "output_limited": Termination.OUTPUT_LIMIT,
            "cancelled": Termination.CANCELLED,
        }[failure]
    )
    assert any(call[0] == "kill" for call in fake.calls)
    assert not fake.exists
    assert result.logs_complete is False


@pytest.mark.parametrize("attribute", ["fail_create", "fail_attach"])
def test_container_creation_or_attachment_errors_still_cleanup(
    tmp_path: Path,
    owned_repository: Path,
    policy: SandboxPolicy,
    attribute: str,
) -> None:
    fake = FakeDocker(policy)
    setattr(fake, attribute, True)
    result = runner(tmp_path, fake).run(
        owned_repository,
        CommandSpec(argv=("python", "main.py")),
        resource_limits=policy,
    )
    assert result.termination is Termination.INFRA_ERROR
    assert not fake.exists


def test_cleanup_failure_is_explicit_and_carries_persisted_evidence(
    tmp_path: Path,
    owned_repository: Path,
    policy: SandboxPolicy,
) -> None:
    fake = FakeDocker(policy)
    fake.fail_cleanup = True
    with pytest.raises(SandboxCleanupError) as failure:
        runner(tmp_path, fake).run(
            owned_repository,
            CommandSpec(argv=("python", "main.py")),
            resource_limits=policy,
        )
    assert failure.value.result.termination is Termination.INFRA_ERROR
    assert sum(call[0] == "rm" for call in fake.calls) == 2


@pytest.mark.parametrize(
    "override",
    [
        {"Privileged": True},
        {"Memory": 0},
        {"MemorySwap": -1},
        {"NanoCpus": 0},
        {"PidsLimit": 0},
        {"ReadonlyRootfs": False},
        {"NetworkMode": "host"},
        {"Binds": ["/home:/host"]},
        {"Devices": ["/dev/sda"]},
        {"PidMode": "host"},
        {"IpcMode": "host"},
        {"CgroupnsMode": "host"},
        {"CapDrop": []},
        {"SecurityOpt": ["seccomp=unconfined"]},
    ],
)
def test_actual_docker_configuration_is_checked_before_target_input(
    tmp_path: Path,
    owned_repository: Path,
    policy: SandboxPolicy,
    override: dict[str, object],
) -> None:
    fake = FakeDocker(policy)
    fake.host_overrides = override
    result = runner(tmp_path, fake).run(
        owned_repository,
        CommandSpec(argv=("python", "main.py")),
        resource_limits=policy,
    )
    assert result.termination is Termination.INFRA_ERROR
    assert not any(call[0] == "attach" for call in fake.calls)
    assert not fake.exists


@pytest.mark.parametrize(
    "override",
    [
        {"memory": "max"},
        {"pids": "max"},
        {"cpu": "max 100000"},
        {"swap": "max"},
        {"uid": 0},
        {"capabilities": "1"},
        {"seccomp": "0"},
        {"no_new_privileges": "0"},
    ],
)
def test_kernel_enforcement_must_be_verified_before_target_input(
    tmp_path: Path,
    owned_repository: Path,
    policy: SandboxPolicy,
    override: dict[str, object],
) -> None:
    fake = FakeDocker(policy)
    fake.probe_overrides = override
    result = runner(tmp_path, fake).run(
        owned_repository,
        CommandSpec(argv=("python", "main.py")),
        resource_limits=policy,
    )
    assert result.termination is Termination.INFRA_ERROR
    assert result.resource_usage is not None and not result.resource_usage.cgroup_verified
    assert not fake.exists


def test_missing_daemon_capabilities_never_fall_back_to_host_execution(
    tmp_path: Path,
    owned_repository: Path,
    policy: SandboxPolicy,
) -> None:
    fake = FakeDocker(policy)
    fake.capabilities = {"PidsLimit": False}
    result = runner(tmp_path, fake).run(
        owned_repository,
        CommandSpec(argv=("python", "main.py")),
        resource_limits=policy,
    )
    assert result.termination is Termination.INFRA_ERROR
    assert not any(call[0] == "create" for call in fake.calls)


def test_install_and_test_are_independent_offline_containers(
    tmp_path: Path,
    owned_repository: Path,
    policy: SandboxPolicy,
) -> None:
    fake = FakeDocker(policy)
    backend = runner(tmp_path, fake)
    installed = backend.run_install(
        owned_repository,
        CommandSpec(argv=("python", "-m", "pip", "--version")),
        resource_limits=policy,
    )
    tested = backend.run(
        owned_repository, CommandSpec(argv=("python", "main.py")), resource_limits=policy
    )
    assert installed.phase == "install" and tested.phase == "test"
    assert installed.id != tested.id
    assert sum(call[0] == "create" for call in fake.calls) == 2
    assert not fake.exists


def test_mutable_image_tags_and_sensitive_env_are_refused(
    tmp_path: Path,
    owned_repository: Path,
    policy: SandboxPolicy,
) -> None:
    with pytest.raises(PolicyViolation, match="digest"):
        DockerSandboxRunner(image="python:latest", artifacts=FileArtifactStore(tmp_path / "data"))
    with pytest.raises(PolicyViolation, match="environment"):
        runner(tmp_path, FakeDocker(policy)).run(
            owned_repository,
            CommandSpec(argv=("python",), env=(("AWS_SECRET_ACCESS_KEY", "secret"),)),
            resource_limits=policy,
        )


def test_snapshot_uses_committed_files_and_excludes_sensitive_names(owned_repository: Path) -> None:
    (owned_repository / "untracked-secret").write_text("never copy", encoding="utf-8")
    (owned_repository / "main.py").write_text("dirty", encoding="utf-8")
    snapshot = snapshot_repository(owned_repository)
    with tarfile.open(fileobj=io.BytesIO(snapshot.archive)) as archive:
        assert archive.getnames() == ["main.py"]
        stream = archive.extractfile("main.py")
        assert stream is not None and stream.read() == b"print('fixture only')\n"


def test_artifact_store_checks_content_integrity(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path)
    reference = store.put_bytes(b"evidence", media_type="text/plain")
    assert store.read_bytes(reference) == b"evidence"
    (tmp_path / reference.path).write_bytes(b"tampered")
    with pytest.raises(RepoReproError, match="integrity"):
        store.read_bytes(reference)


def test_pipe_capture_is_bounded_and_calls_abort_for_real_timeout() -> None:
    aborted: list[bool] = []
    captured = capture_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=0.1,
        output_limit=1024,
        on_abort=lambda: aborted.append(True),
    )
    assert captured.timed_out and aborted == [True]


def test_real_pipe_output_flood_is_bounded() -> None:
    captured = capture_process(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 5000000)"],
        timeout=5,
        output_limit=1024,
    )
    assert captured.output_limited
    assert len(captured.stdout) + len(captured.stderr) <= 1024


def test_resource_samples_distinguish_decimal_and_binary_units() -> None:
    assert memory_bytes("1MiB") == 1_048_576
    assert memory_bytes("1MB") == 1_000_000
    assert stats_sample({"MemUsage": "8MiB / 2GiB", "CPUPerc": "2.5%", "PIDs": "3"}) == (
        8_388_608,
        2.5,
        3,
    )


def test_cleanup_never_removes_a_container_with_another_owner(
    tmp_path: Path,
    owned_repository: Path,
    policy: SandboxPolicy,
) -> None:
    fake = FakeDocker(policy)
    fake.fail_create = fake.foreign_owner = True
    with pytest.raises(SandboxCleanupError):
        runner(tmp_path, fake).run(
            owned_repository,
            CommandSpec(argv=("python", "main.py")),
            resource_limits=policy,
        )
    assert not any(call[0] == "rm" for call in fake.calls)


def test_unreachable_daemon_is_not_confused_with_a_missing_container() -> None:
    assert not DockerSandboxRunner._not_found(
        ProcessOutput(1, b"", b"dial unix /var/run/docker.sock: no such file or directory")
    )


def test_failed_kill_is_recorded_and_force_removal_still_runs(
    tmp_path: Path,
    owned_repository: Path,
    policy: SandboxPolicy,
) -> None:
    fake = FakeDocker(policy)
    fake.fail_kill = True
    fake.outcome = ProcessOutput(137, b"", b"", timed_out=True)
    result = runner(tmp_path, fake).run(
        owned_repository,
        CommandSpec(argv=("python", "main.py")),
        resource_limits=policy,
    )
    assert result.termination is Termination.INFRA_ERROR
    assert not fake.exists


def test_store_rejects_oversized_artifacts_before_writing(tmp_path: Path) -> None:
    with pytest.raises(RepoReproError, match="size limit"):
        FileArtifactStore(tmp_path, max_artifact_bytes=4).put_bytes(
            b"too large",
            media_type="text/plain",
        )


@pytest.mark.parametrize("script", [_BOOTSTRAP, _PROBE])
def test_trusted_container_helpers_have_valid_syntax_without_host_execution(script: str) -> None:
    ast.parse(script)
