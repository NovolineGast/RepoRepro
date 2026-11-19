"""Flatten a reviewed Python supervisor image plus an official Go toolchain, without running it."""

import argparse
import json
import tarfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

from repo_repro.sandbox.docker_runner import DockerCLI


def prepare(base_image: str, go_image: str, workspace: Path) -> str:
    cli = DockerCLI()

    def run(argv: list[str], timeout: float = 120) -> bytes:
        result = cli.run(argv, timeout=timeout)
        if result.returncode or result.timed_out or result.output_limited:
            raise RuntimeError(
                "Docker image preparation failed: " + result.stderr.decode(errors="replace")
            )
        return result.stdout

    directory = workspace.resolve() / uuid4().hex
    directory.mkdir(parents=True)
    identities: list[str] = []
    for index, image in enumerate((base_image, go_image)):
        info = json.loads(run(["image", "inspect", image]))[0]
        if info.get("Os") != "linux" or info.get("Architecture") != "amd64":
            raise ValueError("reviewed input images must be Linux amd64")
        identities.append(info["Id"])
        name = "repo-repro-go-prep-" + uuid4().hex
        run(["create", "--name", name, "--network=none", "--entrypoint=/bin/true", info["Id"]])
        try:
            run(["export", "--output", str(directory / f"{index}.tar"), name])
        finally:
            run(["rm", name])
    count = 0
    with tarfile.open(directory / "runtime.tar", "w") as output:
        for index in (0, 1):
            with tarfile.open(directory / f"{index}.tar") as source:
                for member in source:
                    name = member.name.removeprefix("./")
                    if index == 1:
                        if not name.startswith("usr/local/go/"):
                            continue
                        # Only platform_test.go uses this upstream test credential.
                        if name == "usr/local/go/src/crypto/x509/platform_root_key.pem":
                            continue
                        relative = name.removeprefix("usr/local/go/")
                        if any(
                            p in {"testdata", "test", "doc", "misc", "api"}
                            for p in relative.split("/")
                        ) or name.endswith("_test.go"):
                            continue
                        if member.isfile():
                            count += 1
                    elif name.startswith("usr/local/go/"):
                        raise ValueError("base image already contains a Go toolchain")
                    if (
                        PurePosixPath(name).is_absolute()
                        or ".." in PurePosixPath(name).parts
                        or "\\" in name
                    ):
                        raise ValueError("unsafe runtime filesystem path")
                    stream = source.extractfile(member) if member.isfile() else None
                    try:
                        output.addfile(member, stream)
                    finally:
                        if stream is not None:
                            stream.close()
    if count < 100:
        raise ValueError("Go input image did not contain a toolchain")
    identity = (
        run(["import", "--platform=linux/amd64", str(directory / "runtime.tar")]).decode().strip()
    )
    (directory / "build-record.json").write_text(
        json.dumps(
            {"input_images": identities, "image": identity, "toolchain_files": count}, indent=2
        ),
        encoding="utf-8",
    )
    return identity


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--go-image", required=True)
    parser.add_argument("--workspace", type=Path, default=Path(".tools/go-image-build"))
    args = parser.parse_args()
    print(prepare(args.base_image, args.go_image, args.workspace))


if __name__ == "__main__":
    main()
