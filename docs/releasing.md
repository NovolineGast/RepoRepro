# Releasing

This repository publishes an alpha toolkit, not an arbitrary-code security boundary. Review the documented scope and [security policy](../SECURITY.md) before preparing a release.

1. Confirm the version in `src/repo_repro/__init__.py` and the dated entry in `CHANGELOG.md`.
2. Run CI, including Docker/Capsule E2E and a clean wheel installation. Inspect any failed or skipped checks.
3. Run the README example with fresh output directories. Record the actual output if updating the demo GIF; do not invent results or use playback timing as a benchmark.
4. Build with `python -m build`, then run `python -m twine check dist/*`. Inspect the wheel and source distribution for required resources and accidental private files.
5. Confirm that the contact in `SECURITY.md` and `CODE_OF_CONDUCT.md` is monitored.
6. Create a GitHub prerelease for the reviewed commit using the matching `v<version>` tag. Attach the wheel, source distribution, SHA-256 sums, and any reviewed public example assets.

Keep model conversations, tool transcripts, local QA reports, private artifacts, test images, cached repositories, and machine-specific design notes out of commits and release assets. `.tools/`, `.repo-repro/`, and `reports/` are local-only directories. Public documentation describes implemented behavior; design drafts do not define an API.

PyPI publication is separate. Verify ownership of the distribution name and configure the intended publisher before uploading. A successful local `pip install` does not establish that a package has been published on PyPI.
