"""Conservative share-time redaction and bounded archive-source inspection."""

import os
import re
from collections.abc import Iterable

from repo_repro.capsule.schema import CapsuleError
from repo_repro.core.exceptions import PolicyViolation

TOKEN = re.compile(
    rb"(?:sk-[A-Za-z0-9_-]{32,}|gh[pousr]_[A-Za-z0-9_]{32,}|github_pat_[A-Za-z0-9_]{32,})"
)
PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?"
    r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----",
    re.S,
)
PRIVATE_KEY_HEADER = re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----\r?\n[A-Za-z0-9+/]")
KEY = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|token|password|passwd|secret|authorization|credential)s?(?:$|[_-])"
)
SECRET_REFERENCE = r"\$\{\{\s*secrets\.[A-Za-z_][A-Za-z0-9_]*\s*\}\}"
ASSIGNMENT = re.compile(
    r"""(?i)(\b(?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|(?:access[_-]?)?token|password|passwd|secret|authorization|credential)s?(?:[_-](?:access[_-]?key|key|token|password|secret))?\b["']?\s*[=:]\s*)(?:["']([^"'\r\n]*)["']|("""
    + SECRET_REFERENCE
    + r"(?=$|[\s,;])|[^\s,;}]+))"
)


class SecretRedactor:
    def __init__(self, secrets: Iterable[str] = (), *, include_environment: bool = True) -> None:
        values = set(value for value in secrets if value)
        if include_environment:
            values.update(value for key, value in os.environ.items() if value and KEY.search(key))
        self.secrets = tuple(sorted(values, key=len, reverse=True))

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        value = TOKEN.sub(b"[REDACTED]", value.encode()).decode()
        value = PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", value)
        value = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", value)
        value = re.sub(r"([A-Za-z][A-Za-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", value)
        return ASSIGNMENT.sub(
            lambda match: (
                match[0]
                if re.fullmatch(SECRET_REFERENCE, match[2] or match[3])
                else match[1] + '"[REDACTED]"'
            ),
            value,
        )

    def value(self, value: object) -> object:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {
                self.text(str(key)): "[REDACTED]" if KEY.search(str(key)) else self.value(item)
                for key, item in value.items()
            }
        if isinstance(value, (tuple, list)):
            return [self.value(item) for item in value]
        return value

    def critical(self, content: bytes, *, assignments: bool = True) -> None:
        if self.binary_sensitive(content):
            raise CapsuleError(
                "replay data contains credentials; use diagnostic-only or remove secrets at source"
            )
        if assignments:
            try:
                text = content.decode("utf-8")
            except UnicodeError:
                return
            if self.text(text) != text:
                raise CapsuleError(
                    "replay data contains sensitive assignments and cannot be rewritten"
                )

    def binary_sensitive(self, content: bytes) -> bool:
        return bool(TOKEN.search(content) or PRIVATE_KEY_HEADER.search(content)) or any(
            secret.encode() in content for secret in self.secrets
        )


def source_files(content: bytes) -> dict[str, bytes]:
    from repo_repro.core.source_archives import source_files as read_source

    try:
        return read_source(content)
    except (PolicyViolation, ValueError) as error:
        raise CapsuleError("invalid source snapshot") from error


def pack_source(files: dict[str, bytes]) -> bytes:
    from repo_repro.core.source_archives import pack_source as pack

    try:
        return pack(files)
    except (PolicyViolation, ValueError) as error:
        raise CapsuleError("cannot pack source snapshot") from error
