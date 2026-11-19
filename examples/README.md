# Examples

The [Go examples](../docs/go.md) cover an owned connector bug and a pinned historical xstrings case, with Capsule v2 export and replay.

Run these commands from a checkout with Repo Repro installed and the virtual environment activated. `issue.json` is a synthetic GitHub-shaped payload; it does not claim that the example URL exists.

```bash
repro issue --issue-json examples/issue.json
```

## Prepare test images

These images are for the owned Python 3.11 demo and integration tests. They are not general project environments. Review the base and wheel inputs before preparing them.

Bash:

```bash
docker pull --platform linux/amd64 python:3.11-slim-bookworm
python -m pip download --only-binary=:all: --python-version=3.11 \
  --dest .tools/demo-wheels pytest==9.2.0 pip==26.3 setuptools==84.2.1
python examples/prepare_pytest_image.py --base-image python:3.11-slim-bookworm \
  --wheelhouse .tools/demo-wheels --json > .tools/demo-images.json
python examples/run_demo.py --images .tools/demo-images.json --output .repo-repro/demo
```

PowerShell:

```powershell
docker pull --platform linux/amd64 python:3.11-slim-bookworm
python -m pip download --only-binary=:all: --python-version=3.11 --dest .tools/demo-wheels pytest==9.2.0 pip==26.3 setuptools==84.2.1
python examples/prepare_pytest_image.py --base-image python:3.11-slim-bookworm --wheelhouse .tools/demo-wheels --json | Set-Content -Encoding utf8 .tools/demo-images.json
python examples/run_demo.py --images .tools/demo-images.json --output .repo-repro/demo
```

The helper exports a stopped base container, updates packaging/framework files with reviewed pure-wheel data, omits the unused GnuTLS library with embedded self-test keys, and imports fresh single-layer images. No wheel or target repository code runs on the host. This deliberately drops inherited image configuration; the local sandbox supplies its own launcher and environment. Inputs, omissions, and image IDs are recorded under `.tools/pytest-image-build/`.

The demo prints actual command results and writes `context.json`, `generated.json`, `reproduction.json`, `verification.json`, `case.zip`, a local `policy.json`, and `replay/replay-report.json`. Output directories must be new. Inspect the recorded original/fixed statuses and replay matches; do not interpret them as a benchmark.

The development branch also includes an [AI-driven Unicode demo](../docs/ai-investigation.md). It uses a real configured model to select files and generate or revise a reproducer, followed by independent Docker verification. The calculator GIF above remains the deterministic, model-free example.

## Manual workflow

Run this instead of the one-command demo, using a fresh directory. Bash:

```bash
IMAGE_ID=$(python -c "import json; print(json.load(open('.tools/demo-images.json'))['pytest_image'])")
python examples/create_reproduction_demo.py .repo-repro/manual --image "$IMAGE_ID"
repro generate .repo-repro/manual/context.json \
  --test-file .repo-repro/manual/test_repro.py \
  --artifacts .repo-repro/manual/evidence --output .repo-repro/manual/generated.json
ORIGINAL=$(python -c "import json; print(json.load(open('.repo-repro/manual/commits.json'))['original'])")
FIXED=$(python -c "import json; print(json.load(open('.repo-repro/manual/commits.json'))['fixed'])")
repro reproduce .repo-repro/manual/repository .repo-repro/manual/generated.json \
  .repo-repro/manual/environment.json --commit "$ORIGINAL" \
  --artifacts .repo-repro/manual/evidence --output .repo-repro/manual/reproduction.json
repro verify .repo-repro/manual/repository "$ORIGINAL" "$FIXED" \
  .repo-repro/manual/generated.json .repo-repro/manual/environment.json \
  --artifacts .repo-repro/manual/evidence --output .repo-repro/manual/verification.json
repro capsule export .repo-repro/manual/verification.json .repo-repro/manual/case.zip \
  --artifacts .repo-repro/manual/evidence
repro capsule validate .repo-repro/manual/case.zip
repro capsule replay .repo-repro/manual/case.zip .repo-repro/manual/replay \
  --policy .repo-repro/manual/policy.json
```

In PowerShell, read IDs with `Get-Content -Raw ... | ConvertFrom-Json`, then pass the resulting strings to the same CLI commands. All CLI inputs and policy files are local; Capsule contents do not authorize replay.

## Real Docker tests

Use the helper's two images to test missing-framework behavior as well as pytest execution:

```powershell
$images = Get-Content .tools/demo-images.json -Raw | ConvertFrom-Json
$env:REPRO_DOCKER_TEST_IMAGE = $images.base_image
$env:REPRO_PYTEST_IMAGE = $images.pytest_image
$env:REPRO_REQUIRE_DOCKER = '1'
$env:REPRO_REQUIRE_ENGINE = '1'
python -m pytest tests/integration
```

On Bash, set the same environment variables to the JSON values. CI demonstrates this in [.github/workflows/ci.yml](../.github/workflows/ci.yml).

## Lower-level sandbox example

With an approved image ID and the demo repository:

```bash
python examples/run_sandbox.py --repository .repo-repro/demo/repository \
  --image "$IMAGE_ID" --isolation reviewed_docker \
  --command python -c "print('owned sandbox example')"
```

`--install` records a separate offline installation experiment. Files installed in that disposable container are not carried into later tests. See [sandbox behavior](../docs/sandbox.md).
