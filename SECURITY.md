# Security policy

## Reporting a vulnerability

Email **[xiaozhanyi36@gmail.com](mailto:xiaozhanyi36@gmail.com)**. Please do not disclose an unpatched vulnerability, exploit, credentials, or private repository contents in a public issue.

Include the affected version, platform, a description of the boundary crossed, and a minimal owned proof of concept. Remove live credentials. Agree on a safe transfer method before sending a large or sensitive Capsule. Maintainers will coordinate a fix and public disclosure with the reporter; no response-time guarantee is currently offered.

## Supported versions

Security fixes target the latest 0.1.x release and the main branch. Older versions may require upgrading. This alpha is not independently audited.

## Trust boundaries

Repo Repro parses untrusted Issue text, repository objects, model output, archives, and logs. GitHub acquisition and static analysis do not execute target source. Tests and installation experiments run through the sandbox.

The Docker backend requires reviewed, locally cached immutable images, Linux amd64, cgroup v2, non-root execution, no network or host mounts, a read-only root filesystem, resource limits, and confirmed cleanup. It does not provide gVisor, Firecracker, or a remote VM backend. For hostile or high-risk inputs, run the entire toolkit inside a disposable VM with quotas.

The pytest collector shares a process and writable container environment with target code. A malicious target can tamper with or fabricate evidence. Repeated execution detects observed inconsistencies; it does not prove determinism or authenticate a hostile target's behavior. The caller must review the test and its failure oracle.

The unreleased Go adapter has the same reviewed-target assumption. Its native event stream and
report file are not independently authenticated. It accepts a restricted return-value test shape,
fixes toolchain/module environment settings and disables network fetching; these checks do not
make arbitrary Go packages trustworthy. See the [Go scope and runtime requirements](docs/go.md).

## Capsules and sensitive data

Validation and extraction do not execute files or load images. Replay is a separate explicit operation under a local policy. Hashes provide integrity checks, not publisher authentication. Loading an image also exposes the Docker daemon to archive parsing.

Diagnostic exports redact known secrets and common credential patterns. Detected secrets in replay-critical source, tests, or image layers cause rejection rather than rewriting executable data. This includes embedded self-test private keys in some system libraries. Unknown, encoded, or encrypted secrets can evade pattern matching. Review every Capsule before sharing it.

Original artifact stores preserve raw evidence and may contain secrets. Model-assisted generation sends the selected context to the configured provider. Keep artifact stores, API keys, local configuration, and private Capsules out of version control.

The development-branch investigator additionally sends a filtered file catalog, seed context, and redacted original-version execution feedback to the selected provider. Planning hypotheses are not static facts. Planning corrections and candidate revisions are budgeted; model output cannot widen runtime policy or change the sealed oracle. Fixed-version execution is evaluated separately and is not fed back into test generation.

Language/framework adapters are trusted host-side code registered explicitly by the application. They are not loaded from model output, repositories, or Capsules. Review adapter code before registering it. Core sandbox enforcement and cleanup remain mandatory; adapter runtime inventory cannot overwrite kernel enforcement evidence.

Git clone transfer does not have a cross-platform hard disk quota. Archive and output budgets limit later processing but do not replace acquisition quotas or stronger host isolation.
