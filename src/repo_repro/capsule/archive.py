"""Streaming ZIP integrity checks and transactional extraction. Never execute capsule data."""

import hashlib
import os
import stat
import tempfile
import zipfile
import zlib
from pathlib import Path

from pydantic import ValidationError

from repo_repro.capsule.schema import CapsuleError, CapsuleManifest, json_object, safe_path

MAX_FILES = 10_000
MAX_FILE = 2_147_483_648
MAX_TOTAL = 4_294_967_296
MAX_METADATA = 8_388_608


def _entries(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    entries: dict[str, zipfile.ZipInfo] = {}
    seen: set[str] = set()
    total = 0
    for member in archive.infolist():
        if len(seen) >= MAX_FILES or member.flag_bits & 1:
            raise CapsuleError("too many or encrypted ZIP members")
        name = member.filename.removesuffix("/") if member.is_dir() else member.filename
        safe_path(name)
        if name.casefold() in seen or not (
            name == "reproduction" or name.startswith("reproduction/")
        ):
            raise CapsuleError("duplicate or unexpected archive root")
        seen.add(name.casefold())
        kind = stat.S_IFMT(member.external_attr >> 16)
        if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or member.external_attr & 0x400:
            raise CapsuleError("ZIP links/devices/reparse points are forbidden")
        if member.file_size > MAX_FILE or member.file_size < 0:
            raise CapsuleError("ZIP member exceeds size limit")
        if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise CapsuleError("unsupported ZIP compression")
        if member.file_size > max(member.compress_size, 1) * 1000:
            raise CapsuleError("suspicious ZIP compression ratio")
        total += member.file_size
        if total > MAX_TOTAL:
            raise CapsuleError("ZIP exceeds total expanded size budget")
        if not member.is_dir():
            if not name.startswith("reproduction/"):
                raise CapsuleError("root must be a directory")
            entries[name.removeprefix("reproduction/")] = member
    file_aliases = {key.casefold() for key in entries}
    for path in entries:
        if any(
            ("/".join(path.split("/")[:i])).casefold() in file_aliases
            for i in range(1, len(path.split("/")))
        ):
            raise CapsuleError("file/directory collision")
    return entries


def _small(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> bytes:
    if member.file_size > MAX_METADATA:
        raise CapsuleError("metadata exceeds size limit")
    with archive.open(member) as stream:
        data = stream.read(MAX_METADATA + 1)
    if len(data) != member.file_size:
        raise CapsuleError("incomplete metadata")
    return data


def _verify(archive: zipfile.ZipFile) -> tuple[CapsuleManifest, dict[str, zipfile.ZipInfo]]:
    entries = _entries(archive)
    if not {"repro.yaml", "checksums.sha256"}.issubset(entries):
        raise CapsuleError("missing capsule manifest/checksums")
    manifest = CapsuleManifest.model_validate(json_object(_small(archive, entries["repro.yaml"])))
    catalog = {item.path: item for item in manifest.files}
    if set(entries) != {*catalog, "repro.yaml", "checksums.sha256"}:
        raise CapsuleError("unlisted or missing payload files")
    for name, member in entries.items():
        if name.endswith((".json", ".yaml", ".md")) and member.file_size > MAX_METADATA:
            raise CapsuleError("metadata payload exceeds 8 MiB slot limit")
        if name.startswith("sources/") and member.file_size > 33_554_432:
            raise CapsuleError("source payload exceeds 32 MiB slot limit")
        if (
            name in {"tests/test_reproduction.py", "tests/test_reproduction.go"}
            and member.file_size > 131_072
        ):
            raise CapsuleError("test payload exceeds text size limit")
    checksums: dict[str, str] = {}
    for line in _small(archive, entries["checksums.sha256"]).decode("ascii").splitlines():
        if len(line) < 67 or line[64:66] != "  ":
            raise CapsuleError("malformed checksum line")
        expected_digest, path = line[:64], line[66:]
        safe_path(path)
        if (
            path in checksums
            or len(expected_digest) != 64
            or any(char not in "0123456789abcdef" for char in expected_digest)
        ):
            raise CapsuleError("duplicate or invalid checksum")
        checksums[path] = expected_digest
    if set(checksums) != set(entries) - {"checksums.sha256"}:
        raise CapsuleError("checksum coverage differs from payload")
    for path, member in entries.items():
        if path == "checksums.sha256":
            continue
        digest = hashlib.sha256()
        count = 0
        with archive.open(member) as stream:
            while chunk := stream.read(131_072):
                count += len(chunk)
                if count > member.file_size:
                    raise CapsuleError("ZIP expanded size differs from metadata")
                digest.update(chunk)
        if count != member.file_size or digest.hexdigest() != checksums[path]:
            raise CapsuleError("file checksum mismatch")
        if path in catalog and (
            count != catalog[path].size_bytes or digest.hexdigest() != catalog[path].sha256
        ):
            raise CapsuleError("file differs from manifest catalog")
    return manifest, entries


def validate_capsule(path: Path) -> CapsuleManifest:
    try:
        with zipfile.ZipFile(path) as archive:
            return _verify(archive)[0]
    except (OSError, ValueError, ValidationError, zipfile.BadZipFile, zlib.error) as error:
        raise CapsuleError("invalid ZIP capsule or unsupported schema") from error


def extract_capsule(path: Path, destination: Path) -> CapsuleManifest:
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise CapsuleError("extraction destination must not already exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(path) as archive:
            manifest, entries = _verify(archive)
            with tempfile.TemporaryDirectory(
                prefix=".capsule-import-", dir=destination.parent
            ) as work:
                root = Path(work) / "reproduction"
                root.mkdir()
                for name, member in entries.items():
                    target = root / name
                    if not target.resolve().is_relative_to(root.resolve()):
                        raise CapsuleError("extraction path escaped destination")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, target.open("xb") as output:
                        count = 0
                        digest = hashlib.sha256()
                        while chunk := source.read(131_072):
                            count += len(chunk)
                            if count > member.file_size:
                                raise CapsuleError("expanded file size changed")
                            output.write(chunk)
                            digest.update(chunk)
                    if count != member.file_size:
                        raise CapsuleError("extraction size changed after validation")
                    expected = next(
                        (item.sha256 for item in manifest.files if item.path == name), None
                    )
                    if expected is not None and digest.hexdigest() != expected:
                        raise CapsuleError("payload changed during extraction")
                if (
                    root.resolve().parent != Path(work).resolve()
                    or destination.parent.resolve() != Path(work).resolve().parent
                ):
                    raise CapsuleError("unexpected staging location")
                os.rename(root, destination)
            return manifest
    except (OSError, ValueError, ValidationError, zipfile.BadZipFile, zlib.error) as error:
        raise CapsuleError("cannot safely extract capsule") from error
