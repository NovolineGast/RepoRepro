# Go support (unreleased)

The development branch supports one root Go module, pure Go, standard-library tests,
and exported functions returning a single comparable scalar. Python/pytest remains supported.
The v0.1.0 release assets do not contain this adapter.

## Scope

`repro analyze --language go` reads committed `.go` files and `go.mod` without running Go.
`generate`, `reproduce`, `verify`, and `investigate` select the adapter from the sealed oracle's
`language`. `repro adapters` lists `go` / `go-test` and `python` / `pytest`.

Go tests use this exact restricted shape:

```go
package repro
import (
    "testing"
    target "example.com/connector"
)
func TestRepro(t *testing.T) {
    got := target.Normalize("_")
    if got != "_" {
        t.Errorf("connector preserved: got %v", got)
    }
}
```

Arguments and the expected value are string, numeric or boolean literals. There is one direct
repository call and one comparison of its result. The assertion message contains the caller's
failure text and exactly one `%v`. Helpers, comments, extra imports, mutation, skips, subtests,
fixtures and arbitrary Go programs are rejected. This is a small recognized language, not a
general Go parser or a guarantee about repository code.

An oracle includes `"language": "go"`, `"node_id": "TestRepro"`, and
`"exception_type": "GoAssertionFailure"`, plus the usual Issue quote and behavior contract.
The generated test runs in the reserved package and imports the selected source package.
Unexported identifiers, `internal` packages, multiple return values, CGO, multiple modules and
Go workspaces are outside this first implementation.

## Runtime and evidence

Use a reviewed Linux amd64 image containing `/usr/local/bin/python3` for the existing trusted
supervisor and `/usr/local/go/bin/go` for tests. Go on the host is unnecessary. Toolchain and
dependency preparation happen before execution; the runner never downloads them.

The launcher fixes `GOTOOLCHAIN=local`, `GOWORK=off`, `CGO_ENABLED=0`, `GOENV=off`,
`GOPROXY=off`, `GOSUMDB=off` and `GOFLAGS=-mod=readonly`. Preloaded dependencies may reside
in the image's `/opt/go-mod`. Automatic dependency preparation is not implemented.
Compilation uses bounded container workspace storage. Docker policy and kernel checks remain
the same as Python. No target code executes on the host.

Each attempt runs `go test -json -count=1 -p=1 -parallel=1 -timeout=30s -run=^TestRepro$`
on the reserved package. The outer sandbox deadline also covers compilation. Native target and
package events, assertion location, process exit and stream completeness must agree. Go has
one normalized call phase; pytest setup/teardown phases are not synthesized. Build/dependency
failures, panics, skips, absent targets, truncated output and timeouts cannot establish a matching
assertion failure. Repetitions still detect inconsistent outcomes and runtime fingerprints.
The existing `pytest_report` wire field carries the framework report; internal callers use
`test_report`.

## Owned example

First prepare the reviewed Python fixture images using the [README demo](../README.md#run-the-complete-local-demo).
Then, from the checkout, use Bash:

```bash
docker pull --platform linux/amd64 golang:1.27-bookworm
base=$(python -c 'import json; print(json.load(open(".tools/demo-images.json"))["base_image"])')
go_image=$(python examples/prepare_go_image.py --base-image "$base" --go-image golang:1.27-bookworm)
python -m examples.create_go_demo --image "$go_image" --output .repo-repro/go-demo
original=$(python -c 'import json; print(json.load(open(".repo-repro/go-demo/commits.json"))["original"])')
fixed=$(python -c 'import json; print(json.load(open(".repo-repro/go-demo/commits.json"))["fixed"])')
repro generate .repo-repro/go-demo/context.json --test-file .repo-repro/go-demo/test.go \
  --artifacts .repo-repro/go-demo/artifacts --output .repo-repro/go-demo/generated.json
repro verify .repo-repro/go-demo/repository "$original" "$fixed" \
  .repo-repro/go-demo/generated.json .repo-repro/go-demo/environment.json \
  --artifacts .repo-repro/go-demo/artifacts --output .repo-repro/go-demo/result.json
repro capsule export .repo-repro/go-demo/result.json .repo-repro/go-demo/case.zip \
  --artifacts .repo-repro/go-demo/artifacts
python -c 'import json; d=json.load(open(".repo-repro/go-demo/environment.json")); json.dump(d["policy"],open(".repo-repro/go-demo/policy.json","w"))'
repro capsule replay .repo-repro/go-demo/case.zip .repo-repro/go-replay \
  --policy .repo-repro/go-demo/policy.json
```

Use fresh output/replay directories. The helper flattens reviewed image files without starting
their containers, removes unused Go tests/testdata/docs and the x509 platform-test private key,
and records immutable input/output
image IDs locally. Its output is a fixture, not a general project image builder.

For AI planning and revision, use the generated `issue.json`, `oracle.json` and environment:

```bash
repro investigate .repo-repro/go-demo/repository .repo-repro/go-demo/issue.json \
  .repo-repro/go-demo/oracle.json .repo-repro/go-demo/environment.json \
  --ref "$original" --fixed-commit "$fixed" --model MODEL_ID --base-url BASE_URL \
  --artifacts .repo-repro/go-demo/artifacts --output .repo-repro/go-demo/investigation.json
```

Set `OPENAI_API_KEY` in the environment. Provider requirements and bounded planning/candidate
budgets are described in the [AI guide](ai-investigation.md). CI uses explicitly controlled model
responses with real containers; that does not establish real-model quality for Go.

## Historical public case

[huandu/xstrings #64](https://github.com/huandu/xstrings/issues/64) describes a connector-only
string whose final rune is duplicated. The example pins original commit
`82cd3c3cf9e0c01f474e3f88a9c65ab60eb39e79` and fix
`a9fcd95229fb6b76828e4a6882474767da2bfb20`.

```bash
git clone https://github.com/huandu/xstrings.git .tools/xstrings
python -m examples.create_go_public_case --repository .tools/xstrings \
  --image "$go_image" --output .repo-repro/go-public
```

This prepares a caller-owned test and a short attributed Issue excerpt. Use the same generate,
verify, export and replay commands with the public paths and pinned commits. It neither patches
upstream source nor runs upstream tests on the host.

## Capsule compatibility

Go uses Capsule **2.0.0**, the `go` / `go-test` pair, `tests/test_reproduction.go`, and the local
trusted Go launcher. Python keeps Capsule **1.0.x**, its existing layout and helper hashes.
The validator rejects mismatched version/language/framework/command/test-path/oracle pairs.
Replay selects only built-in local implementations, checks helper hashes and enforces local
policy. Capsule metadata cannot import a plugin or replace the runner.

## Tests

```bash
python -m pytest tests/test_go.py
REPRO_GO_IMAGE="$go_image" REPRO_REQUIRE_GO=1 python -m pytest tests/integration/test_go.py
```

The integration suite covers controlled AI feedback, real original/fixed runs, v2 export and
replay, compilation failures, missing dependencies, panics and external deadlines.
