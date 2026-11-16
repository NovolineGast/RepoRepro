"""A fail-closed Docker backend with no host mounts and externally supervised timeouts."""

import base64
import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from repo_repro.core.exceptions import ExecutionBackendError, PolicyViolation, SandboxError
from repo_repro.core.models import (
    CommandSpec,
    ExecutionResult,
    RepositoryRevision,
    ResourceUsage,
    SandboxPolicy,
    Termination,
)
from repo_repro.core.source_archives import pack_source, source_files
from repo_repro.reproduction.models import PytestRunRequest, PytestSnapshotRequest
from repo_repro.reproduction.runtime import injected_files
from repo_repro.reproduction.safety import check_test_code
from repo_repro.sandbox.metrics import stats_sample
from repo_repro.sandbox.process import ProcessOutput, capture_process
from repo_repro.sandbox.snapshot import SourceSnapshot, snapshot_repository
from repo_repro.storage.artifacts import FileArtifactStore

# This trusted supervisor starts before repository bytes arrive. It never imports target modules.
_BOOTSTRAP = r"""
import base64, json, os, pathlib, subprocess, sys, tarfile
request = json.loads(sys.argv[1])
root = pathlib.Path('/workspace')
count = total = 0
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
    for member in archive:
        parts = member.name.split('/')
        if (not member.isfile() or any(p in ('', '.', '..') for p in parts)
            or member.name.startswith('/') or '\\' in member.name or ':' in member.name):
            raise RuntimeError('unsafe input archive')
        count += 1
        total += member.size
        if count > 5000 or total > 33554432:
            raise RuntimeError('input archive limit exceeded')
        destination = root.joinpath(*parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = archive.extractfile(member)
        if source is None:
            raise RuntimeError('missing archive payload')
        with source, destination.open('xb') as target:
            while chunk := source.read(65536):
                target.write(chunk)
os.makedirs('/tmp/repro-home', exist_ok=True)
environment = {
    'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/tmp/repro-home', 'TMPDIR': '/tmp',
    'TZ': 'UTC', 'LANG': 'C.UTF-8', 'PYTHONNOUSERSITE': '1',
    'PYTHONDONTWRITEBYTECODE': '1', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
    'PYTEST_ADDOPTS': '', 'PIP_CONFIG_FILE': '/dev/null', 'PIP_NO_INDEX': '1',
    'PIP_DISABLE_PIP_VERSION_CHECK': '1',
}
environment.update(request['env'])
try:
    result = subprocess.run(request['argv'], cwd=request['cwd'], env=environment,
                            stdin=subprocess.DEVNULL, check=False)
    exit_code = result.returncode if result.returncode >= 0 else 128 - result.returncode
except OSError as error:
    print('sandbox command could not start: ' + str(error), file=sys.stderr)
    exit_code = 127
if request.get('report_marker'):
    try:
        with open('/tmp/repo-repro-pytest.json', 'rb') as stream:
            report = stream.read(262145)
        if len(report) <= 262144:
            print('\n' + request['report_marker'] + base64.b64encode(report).decode(), flush=True)
    except OSError:
        pass
sys.exit(exit_code)
"""

_PROBE = r"""
import importlib.metadata, json, os, pathlib, sys, sysconfig
root = pathlib.Path('/sys/fs/cgroup')
lines = pathlib.Path('/proc/self/status').read_text().splitlines()
status = dict(line.split(':', 1) for line in lines)
package_paths = list({sysconfig.get_path('purelib'), sysconfig.get_path('platlib')})
print(json.dumps({
    'memory': (root/'memory.max').read_text().strip(),
    'swap': (root/'memory.swap.max').read_text().strip(),
    'pids': (root/'pids.max').read_text().strip(),
    'cpu': (root/'cpu.max').read_text().strip(),
    'uid': os.geteuid(), 'capabilities': status.get('CapEff', '').strip(),
    'no_new_privileges': status.get('NoNewPrivs', '').strip(),
    'seccomp': status.get('Seccomp', '').strip(),
    'workspace_bytes': os.statvfs('/workspace').f_blocks * os.statvfs('/workspace').f_frsize,
    'python_version': sys.version,
    'platform': list(os.uname()),
    'packages': sorted((dist.metadata.get('Name', ''), dist.version)
                       for dist in importlib.metadata.distributions(
                           path=package_paths)),
}))
"""


class DockerTransport(Protocol):
    def run(self, arguments: Sequence[str], *, timeout: float = 15) -> ProcessOutput: ...

    def attach(
        self,
        name: str,
        payload: bytes,
        *,
        timeout: float,
        output_limit: int,
        before_input: Callable[[], None],
        on_abort: Callable[[], None],
    ) -> ProcessOutput: ...


class DockerCLI:
    def __init__(self, executable: str | None = None) -> None:
        candidate = executable or shutil.which("docker")
        if candidate is None and os.name == "nt":
            installed = Path(os.environ.get("LOCALAPPDATA", "")) / (
                "Programs/DockerDesktop/resources/bin/docker.exe"
            )
            candidate = str(installed) if installed.is_file() else None
        self.executable = candidate

    def run(self, arguments: Sequence[str], *, timeout: float = 15) -> ProcessOutput:
        if self.executable is None:
            raise SandboxError("Docker CLI is not installed", stage="sandbox")
        return capture_process(
            [self.executable, *arguments],
            timeout=timeout,
            output_limit=2_097_152,
        )

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
        if self.executable is None:
            raise SandboxError("Docker CLI is not installed", stage="sandbox")
        return capture_process(
            [self.executable, "start", "--attach", "--interactive", "--detach-keys=", name],
            payload=payload,
            timeout=timeout,
            output_limit=output_limit,
            before_input=before_input,
            on_abort=on_abort,
        )


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


class SandboxCleanupError(ExecutionBackendError):
    """Cleanup could not be confirmed; the complete attempted result is retained."""

    code = "SANDBOX_CLEANUP_FAILED"

    def __init__(self, result: ExecutionResult) -> None:
        super().__init__(
            f"container cleanup could not be confirmed; execution {result.id}",
            result,
            stage="cleanup",
        )


class DockerSandboxRunner:
    def __init__(
        self,
        *,
        image: str,
        artifacts: FileArtifactStore,
        transport: DockerTransport | None = None,
    ) -> None:
        if not re.fullmatch(r"(?:[A-Za-z0-9./:_-]+@)?sha256:[0-9a-f]{64}", image):
            raise PolicyViolation("select an already-present, reviewed image by digest or image ID")
        self.image = image
        self.artifacts = artifacts
        self.transport = transport or DockerCLI()

    def run(
        self,
        repository_path: Path,
        command: CommandSpec,
        *,
        timeout: int | None = None,
        resource_limits: SandboxPolicy,
    ) -> ExecutionResult:
        return self._run(repository_path, command, resource_limits, timeout, "test")

    def run_install(
        self,
        repository_path: Path,
        command: CommandSpec,
        *,
        timeout: int | None = None,
        resource_limits: SandboxPolicy,
    ) -> ExecutionResult:
        """Run a distinct, offline installation experiment in a fresh disposable container."""
        return self._run(repository_path, command, resource_limits, timeout, "install")

    def run_test(self, request: PytestRunRequest) -> ExecutionResult:
        check_test_code(request.test_code)
        return self._run(
            request.repository_path,
            CommandSpec(argv=("python", "-I", "/workspace/repo_repro_generated/launch.py")),
            request.policy,
            None,
            "test",
            test_request=request,
        )

    def read_output(self, result: ExecutionResult) -> tuple[str, str]:
        return (
            self.artifacts.read_bytes(result.stdout).decode("utf-8", errors="replace"),
            self.artifacts.read_bytes(result.stderr).decode("utf-8", errors="replace"),
        )

    def run_snapshot(self, request: PytestSnapshotRequest) -> ExecutionResult:
        check_test_code(request.test_code)
        content = source_files(self.artifacts.read_bytes(request.source))
        if any(path.startswith("repo_repro_generated/") for path in content):
            raise PolicyViolation("snapshot must not supply tool scripts")
        overlay = injected_files(request.test_code)
        if len(content) + len(overlay) > 5000:
            raise PolicyViolation("source snapshot has too many files")
        content.update(overlay)
        prepared = SourceSnapshot(
            request.commit,
            pack_source(content),
            (),
            tuple(
                (name, hashlib.sha256(value).hexdigest()) for name, value in sorted(overlay.items())
            ),
        )
        original_request = PytestRunRequest(
            repository_path=Path("."),
            commit=request.commit,
            test_code=request.test_code,
            policy=request.policy,
            run_id=request.run_id,
            plan_id=request.plan_id,
            repetition=request.repetition,
        )
        return self._run(
            Path("."),
            CommandSpec(argv=("python", "-I", "/workspace/repo_repro_generated/launch.py")),
            request.policy,
            None,
            "test",
            test_request=original_request,
            prepared_source=prepared,
        )

    def _json(self, arguments: Sequence[str]) -> object:
        result = self.transport.run(arguments)
        if result.returncode or result.timed_out or result.output_limited:
            message = f"Docker {arguments[0]} failed"
            if arguments[0] == "info":
                message += (
                    ". Check `docker version` and `docker context show`, then confirm "
                    "the intended Linux Docker daemon is running."
                )
            raise SandboxError(message, stage="sandbox")
        try:
            value: object = json.loads(result.stdout)
            return value
        except (ValueError, RecursionError) as error:
            raise SandboxError(
                "Docker returned invalid or incomplete JSON", stage="sandbox"
            ) from error

    def _inspect(self, name: str) -> dict[str, object]:
        value = self._json(["inspect", name])
        if not isinstance(value, list) or len(value) != 1:
            raise SandboxError("Docker inspect returned an unexpected result", stage="sandbox")
        return _mapping(value[0])

    def _preflight(self) -> dict[str, object]:
        info = _mapping(self._json(["info", "--format", "{{json .}}"]))
        if info.get("OSType") != "linux" or str(info.get("CgroupVersion")) != "2":
            raise SandboxError("Linux containers with cgroup v2 are required", stage="preflight")
        for capability in ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit"):
            if info.get(capability) is not True:
                raise SandboxError(f"Docker cannot enforce {capability}", stage="preflight")
        image = self._json(["image", "inspect", self.image])
        if not isinstance(image, list) or len(image) != 1:
            raise SandboxError("approved image is not available locally", stage="preflight")
        image_info = _mapping(image[0])
        configuration = _mapping(image_info.get("Config"))
        if image_info.get("Os") != "linux" or image_info.get("Architecture") != "amd64":
            raise SandboxError(
                "the current backend requires a Linux amd64 image", stage="preflight"
            )
        if configuration.get("Volumes") or configuration.get("OnBuild"):
            raise PolicyViolation("images with VOLUME or ONBUILD declarations are refused")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(image_info.get("Id", ""))):
            raise SandboxError("Docker image has no valid immutable ID", stage="preflight")
        return {
            "engine_version": info.get("ServerVersion"),
            "security_options": info.get("SecurityOptions"),
            "kernel": info.get("KernelVersion"),
            "operating_system": info.get("OperatingSystem"),
            "cgroup_version": "2",
            "image_id": image_info["Id"],
            "image_reference": self.image,
        }

    def _create_arguments(
        self,
        name: str,
        command: CommandSpec,
        policy: SandboxPolicy,
        report_marker: str | None = None,
    ) -> list[str]:
        request = json.dumps(
            {
                "argv": command.argv,
                "cwd": command.cwd,
                "env": dict(command.env),
                "report_marker": report_marker,
            }
        )
        return [
            "create",
            "--name",
            name,
            "--label",
            "repo-repro.managed=true",
            "--label",
            f"repo-repro.execution={name.removeprefix('repo-repro-')}",
            "--interactive",
            "--pull=never",
            "--user=10001:10001",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges=true",
            "--ipc=private",
            "--cgroupns=private",
            "--no-healthcheck",
            "--log-driver=none",
            "--restart=no",
            f"--cpus={policy.cpus}",
            f"--memory={policy.memory_bytes}",
            f"--memory-swap={policy.memory_bytes}",
            f"--pids-limit={policy.pids}",
            "--ulimit=nofile=1024:1024",
            "--shm-size=16777216",
            "--tmpfs",
            f"/workspace:rw,exec,nosuid,nodev,size={policy.workspace_bytes_limit},uid=10001,gid=10001,mode=0700",
            "--tmpfs",
            "/tmp:rw,exec,nosuid,nodev,size=67108864,uid=10001,gid=10001,mode=0700",
            "--workdir=/workspace",
            "--env=HOME=/tmp/repro-home",
            "--env=LD_PRELOAD=",
            "--env=LD_AUDIT=",
            "--env=LD_LIBRARY_PATH=",
            "--env=PYTHONPATH=",
            "--env=PYTHONHOME=",
            "--entrypoint=/usr/local/bin/python3",
            self.image,
            "-I",
            "-S",
            "-u",
            "-c",
            _BOOTSTRAP,
            request,
        ]

    def _check_configuration(
        self,
        container: dict[str, object],
        policy: SandboxPolicy,
        name: str,
    ) -> None:
        host = _mapping(container.get("HostConfig"))
        config = _mapping(container.get("Config"))
        labels = _mapping(config.get("Labels"))
        if labels.get("repo-repro.managed") != "true" or (
            labels.get("repo-repro.execution") != name.removeprefix("repo-repro-")
        ):
            raise PolicyViolation("container ownership labels were not applied")
        expected: dict[str, object] = {
            "Privileged": False,
            "ReadonlyRootfs": True,
            "NetworkMode": "none",
            "Memory": policy.memory_bytes,
            "MemorySwap": policy.memory_bytes,
            "NanoCpus": round(policy.cpus * 1_000_000_000),
            "PidsLimit": policy.pids,
        }
        if any(host.get(key) != value for key, value in expected.items()):
            raise PolicyViolation("Docker did not apply the requested isolation/resource settings")
        if config.get("User") != "10001:10001" or config.get("Tty") is not False:
            raise PolicyViolation("container identity or output-channel settings are unsafe")
        if (
            host.get("Binds")
            or host.get("Mounts")
            or host.get("Devices")
            or host.get("PortBindings")
        ):
            raise PolicyViolation("host mounts, devices and port publishing are forbidden")
        if host.get("CapAdd") or host.get("PidMode") not in ("", "private"):
            raise PolicyViolation("additional capabilities or host PID namespace are forbidden")
        if host.get("IpcMode") != "private" or host.get("CgroupnsMode") != "private":
            raise PolicyViolation("private IPC and cgroup namespaces are required")
        cap_drop = host.get("CapDrop")
        security = host.get("SecurityOpt")
        if not isinstance(cap_drop, list) or "ALL" not in [str(item).upper() for item in cap_drop]:
            raise PolicyViolation("all Linux capabilities must be dropped")
        if not isinstance(security, list) or not any(
            option
            in {
                "no-new-privileges",
                "no-new-privileges=true",
                "no-new-privileges:true",
            }
            for option in security
        ):
            raise PolicyViolation("no-new-privileges was not enabled")
        mounts = container.get("Mounts", [])
        if not isinstance(mounts, list) or any(
            _mapping(mount).get("Type") != "tmpfs"
            or _mapping(mount).get("Destination") not in {"/workspace", "/tmp"}
            for mount in mounts
        ):
            raise PolicyViolation("unexpected container mount")

    def _probe(self, name: str, policy: SandboxPolicy) -> dict[str, object]:
        # Wait for the trusted supervisor to start before supplying any target bytes.
        deadline = time.monotonic() + 10
        while not _mapping(self._inspect(name).get("State")).get("Running"):
            if time.monotonic() > deadline:
                raise SandboxError("container supervisor did not start", stage="start")
            time.sleep(0.05)
        value = _mapping(
            self._json(
                [
                    "exec",
                    name,
                    "/usr/local/bin/python3",
                    "-I",
                    "-S",
                    "-c",
                    _PROBE,
                ]
            )
        )
        packages = value.get("packages")
        if not isinstance(value.get("python_version"), str) or not isinstance(packages, list):
            raise SandboxError("runtime inventory is incomplete", stage="preflight")
        if len(packages) > 2000 or any(
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
            for item in packages
        ):
            raise SandboxError("runtime package inventory is invalid", stage="preflight")
        try:
            quota, period = str(value["cpu"]).split()
            effective_cpu = int(quota) / int(period)
            verified = (
                int(str(value["memory"])) == policy.memory_bytes
                and int(str(value["swap"])) == 0
                and int(str(value["pids"])) == policy.pids
                and math.isclose(effective_cpu, policy.cpus, rel_tol=0.001)
                and value["uid"] == 10001
                and int(str(value["capabilities"]), 16) == 0
                and value["no_new_privileges"] == "1"
                and value["seccomp"] == "2"
                and int(str(value["workspace_bytes"])) <= policy.workspace_bytes_limit + 4095
            )
        except (KeyError, ValueError, ZeroDivisionError) as error:
            raise PolicyViolation(
                "cannot verify cgroup limits and kernel security state"
            ) from error
        if not verified:
            raise PolicyViolation("cgroup limits or process security state do not match policy")
        return value

    def _kill(self, name: str) -> None:
        outcome = self.transport.run(["kill", name], timeout=10)
        if outcome.returncode:
            state = _mapping(self._inspect(name).get("State"))
            if state.get("Running"):
                raise SandboxError("failed to terminate container", stage="cleanup")

    @staticmethod
    def _not_found(response: ProcessOutput) -> bool:
        return (
            response.returncode != 0
            and not response.timed_out
            and re.search(rb"(?i)\bno such (?:object|container)(?::|\s|$)", response.stderr)
            is not None
        )

    def _cleanup(self, name: str, *, creation_uncertain: bool = False) -> bool:
        for _ in range(2):
            before = self.transport.run(["inspect", name], timeout=10)
            if self._not_found(before):
                return not creation_uncertain
            if before.returncode or before.timed_out or before.output_limited:
                return False
            try:
                descriptions: object = json.loads(before.stdout)
                if not isinstance(descriptions, list) or len(descriptions) != 1:
                    return False
                labels = _mapping(_mapping(descriptions[0]).get("Config")).get("Labels")
                if _mapping(labels).get("repo-repro.managed") != "true" or (
                    _mapping(labels).get("repo-repro.execution") != name.removeprefix("repo-repro-")
                ):
                    return False
            except (ValueError, RecursionError):
                return False
            removed = self.transport.run(["rm", "--force", "--volumes", name], timeout=10)
            inspected = self.transport.run(["inspect", name], timeout=10)
            if self._not_found(inspected):
                return True
            if removed.timed_out:
                continue
        return False

    def _run(
        self,
        repository_path: Path,
        command: CommandSpec,
        policy: SandboxPolicy,
        timeout: int | None,
        phase: Literal["install", "test"],
        *,
        test_request: PytestRunRequest | None = None,
        prepared_source: SourceSnapshot | None = None,
    ) -> ExecutionResult:
        duration_limit = policy.timeout_seconds if timeout is None else timeout
        if not 1 <= duration_limit <= policy.timeout_seconds:
            raise PolicyViolation("timeout must be positive and within local policy")
        for key, value in command.env:
            if not (key == "TZ" and value == "UTC") and not (
                key == "PYTHONHASHSEED" and value.isdigit() and int(value) < 4_294_967_296
            ):
                raise PolicyViolation(
                    "only fixed TZ/PYTHONHASHSEED target environment overrides are allowed"
                )
        overlay = injected_files(test_request.test_code) if test_request else None
        source = prepared_source or snapshot_repository(
            repository_path,
            ref=test_request.commit if test_request else "HEAD",
            injected=overlay,
        )
        revision = RepositoryRevision(
            repository_url="local:git-snapshot",
            commit=source.commit,
            source=self.artifacts.put_bytes(source.archive, media_type="application/x-tar"),
        )
        execution_id, run_id, plan_id = uuid4(), uuid4(), uuid4()
        if test_request:
            run_id, plan_id = test_request.run_id, test_request.plan_id
        report_marker = f"REPO_REPRO_REPORT_{uuid4().hex}:" if test_request else None
        name = f"repo-repro-{execution_id.hex}"
        started_at, started = datetime.now(UTC), time.monotonic()
        backend: dict[str, object] = {
            "container_name": name,
            "policy": policy.model_dump(),
            "deployment_declaration": policy.isolation,
            "excluded_source_paths": source.excluded,
            "phase": phase,
            "injected_files": source.injected,
        }
        output = ProcessOutput(1, b"", b"")
        state: dict[str, object] = {}
        termination = Termination.INFRA_ERROR
        exit_code: int | None = None
        cleanup_confirmed = True
        attempted = False
        creation_uncertain = False
        cgroup_verified = False
        stop_sampling = threading.Event()
        samples: list[tuple[int, float, int]] = []
        sampler: threading.Thread | None = None

        def sample() -> None:
            while not stop_sampling.is_set():
                try:
                    measured = self.transport.run(
                        ["stats", "--no-stream", "--format", "{{json .}}", name],
                        timeout=2,
                    )
                    if measured.returncode == 0 and not measured.output_limited:
                        item = stats_sample(json.loads(measured.stdout))
                        if not stop_sampling.is_set() and len(samples) < 120 and item[2] > 0:
                            samples.append(item)
                except (OSError, ValueError, OverflowError, SandboxError):
                    pass
                stop_sampling.wait(0.5)

        try:
            backend.update(self._preflight())
            attempted = True  # A failed client may still have created the named container.
            created = self.transport.run(
                self._create_arguments(name, command, policy, report_marker)
            )
            creation_uncertain = created.timed_out
            backend["creation_timed_out"] = created.timed_out
            if created.returncode or created.timed_out or created.output_limited:
                output = created
                raise SandboxError("Docker container creation failed", stage="create")
            container = self._inspect(name)
            self._check_configuration(container, policy, name)
            backend["host_configuration"] = container.get("HostConfig")

            def ready() -> None:
                nonlocal cgroup_verified, sampler
                backend["cgroup_probe"] = self._probe(name, policy)
                cgroup_verified = True
                if test_request:
                    framework = _mapping(
                        self._json(
                            [
                                "exec",
                                name,
                                "/usr/local/bin/python3",
                                "-I",
                                "-c",
                                "import json,pytest;"
                                "print(json.dumps({'version':pytest.__version__}))",
                            ]
                        )
                    )
                    if not isinstance(framework.get("version"), str):
                        raise SandboxError("pytest runtime is unavailable", stage="preflight")
                    backend["pytest_version"] = framework["version"]
                sampler = threading.Thread(target=sample, daemon=True)
                sampler.start()

            output = self.transport.attach(
                name,
                source.archive,
                timeout=duration_limit,
                output_limit=policy.output_bytes_limit,
                before_input=ready,
                on_abort=lambda: self._kill(name),
            )
            state = _mapping(self._inspect(name).get("State"))
            backend["final_state"] = state
            raw_code = state.get("ExitCode")
            exit_code = raw_code if type(raw_code) is int else None
            if output.cancelled:
                termination = Termination.CANCELLED
            elif output.timed_out:
                termination = Termination.TIMEOUT
            elif output.output_limited:
                termination = Termination.OUTPUT_LIMIT
            elif state.get("OOMKilled"):
                termination = Termination.OOM
            elif state.get("Running") or state.get("Error") or exit_code is None:
                termination = Termination.INFRA_ERROR
            else:
                termination = Termination.EXITED
        except (SandboxError, PolicyViolation, OSError, ValueError) as error:
            backend["infrastructure_error"] = str(error)
            output = ProcessOutput(
                output.returncode, output.stdout, output.stderr + str(error).encode()
            )
            termination = Termination.INFRA_ERROR
        finally:
            stop_sampling.set()
            if sampler is not None:
                sampler.join(timeout=3)
            if attempted:
                try:
                    cleanup_confirmed = self._cleanup(name, creation_uncertain=creation_uncertain)
                except (SandboxError, OSError, ValueError):
                    cleanup_confirmed = False
            backend["cleanup_confirmed"] = cleanup_confirmed
        if not cleanup_confirmed:
            termination = Termination.INFRA_ERROR
        usage = ResourceUsage(
            memory_limit_bytes=policy.memory_bytes,
            cpu_limit=policy.cpus,
            pids_limit=policy.pids,
            cgroup_verified=cgroup_verified,
            oom_killed=bool(state.get("OOMKilled")),
            memory_sample_bytes=max((item[0] for item in samples), default=None),
            cpu_sample_percent=max((item[1] for item in samples), default=None),
            pids_sample=max((item[2] for item in samples), default=None),
            metric_samples=len(samples),
            metrics_source="docker_stats" if samples else "unavailable",
        )
        captured_stdout = output.stdout[: policy.output_bytes_limit]
        captured_stderr = output.stderr[: max(0, policy.output_bytes_limit - len(captured_stdout))]
        logs_within_budget = len(output.stdout) + len(output.stderr) <= policy.output_bytes_limit
        pytest_report = None
        if report_marker:
            frames = [
                line[len(report_marker) :]
                for line in output.stdout.decode("utf-8", errors="replace").splitlines()
                if line.startswith(report_marker)
            ]
            try:
                if len(frames) == 1 and len(frames[0]) <= 349_528:
                    report_data = base64.b64decode(frames[0], validate=True)
                    if len(report_data) <= 262_144:
                        pytest_report = self.artifacts.put_bytes(
                            report_data, media_type="application/json"
                        )
            except ValueError:
                backend["pytest_report_error"] = "malformed report frame"
            backend["test_sha256"] = (
                hashlib.sha256(test_request.test_code.encode()).hexdigest()
                if test_request
                else None
            )
        result = ExecutionResult(
            id=execution_id,
            run_id=run_id,
            plan_id=plan_id,
            phase=phase,
            repetition=test_request.repetition if test_request else 1,
            revision=revision,
            command=command,
            exit_code=exit_code,
            termination=termination,
            environment_id=uuid4() if cgroup_verified else None,
            resource_usage=usage,
            pytest_report=pytest_report,
            backend_snapshot=self.artifacts.put_bytes(
                json.dumps(backend, sort_keys=True).encode(),
                media_type="application/json",
            ),
            stdout=self.artifacts.put_bytes(captured_stdout, media_type="text/plain"),
            stderr=self.artifacts.put_bytes(captured_stderr, media_type="text/plain"),
            started_at=started_at,
            finished_at=datetime.now(UTC),
            duration_seconds=time.monotonic() - started,
            logs_complete=logs_within_budget
            and not (output.output_limited or output.timed_out or output.cancelled),
        )
        self.artifacts.put_bytes(result.model_dump_json().encode(), media_type="application/json")
        if not cleanup_confirmed:
            raise SandboxCleanupError(result)
        return result
