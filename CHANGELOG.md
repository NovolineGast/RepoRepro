# Changelog

## Unreleased

- Route Python/pytest analysis, planning, generation, execution bundles, runtime inventory, and evidence normalization through explicit language/framework adapters. Preserve the Python Capsule v1 codec and existing import paths.
- Add instance-local adapter registration and the offline `repro adapters` command. No additional language or framework is enabled.

- Add AI Issue diagnosis and bounded Git file selection through `repro investigate`.
- Revise candidate tests using local validation errors and original-version execution evidence; optionally improve a supplied seed test.
- Keep the oracle and environment sealed, deduplicate candidate ASTs, and run fixed-version verification only after selecting the final test.
- Add provider reasoning/output-budget options and a reusable Unicode normalization fixture. Real DeepSeek acceptance covers generation and feedback-driven revision; regular CI uses controlled responses.
- Emit lossless ASCII-escaped JSON on the console so Unicode results do not fail under legacy encodings; file output stays UTF-8.

## 0.1.0 — 2026-11-16

Initial alpha release for Python + pytest.

### Added

- CLI for GitHub Issue retrieval, offline JSON input, and read-only Python repository analysis.
- Reviewed-test sealing and optional OpenAI-compatible candidate generation with audit records.
- Repeated Docker execution with resource limits, runtime inventory, bounded output, and cleanup evidence.
- Single-version reproduction and original/fixed differential verification with flaky and drift detection.
- Capsule v1 offline export, validation, extraction, and explicit local-policy replay.
- An owned bug/fix demo, contributor documentation, and CI for tests, package installation, style, and types.

### Security

- Reject unsafe archive paths, links, ambiguous metadata, invalid checksums, and oversized inputs.
- Redact diagnostic credentials and reject detected secrets in replay-critical data, including private keys in image files.
- Restrict generated tests to the supported simple pytest shape and reject known sources of manufactured failures.

### Known limits

- Python + pytest only; one simple test without fixtures, markers, or parametrization.
- Linux amd64 Docker execution; compatible local runner helpers are required for replay.
- Dependency preparation/freezing, additional isolation backends, SQLite indexing, and generic report generation are not implemented.
- Evidence is not independently authenticated against malicious target code. See [SECURITY.md](SECURITY.md).
