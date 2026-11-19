# Reproducing xstrings #64

[xstrings #64](https://github.com/huandu/xstrings/issues/64) reports that `ToCamelCase` duplicates
the final rune when the input consists entirely of connectors. For input `"_"`, the original result
was `"__"` and the expected result was `"_"`.

| Run | Commit | Observed result |
| --- | --- | --- |
| Original | [`82cd3c3`](https://github.com/huandu/xstrings/commit/82cd3c3cf9e0c01f474e3f88a9c65ab60eb39e79) | Both attempts reached the target assertion and reported `__`. |
| Fixed | [`a9fcd95`](https://github.com/huandu/xstrings/commit/a9fcd95229fb6b76828e4a6882474767da2bfb20) | Both attempts passed with the same test and runtime. |
| Capsule replay | Both pinned commits | `MATCHED` for behavior and environment. |

The test is a single call to `target.ToCamelCase("_")` followed by a comparison with `"_"`.
The original failure message was `connector preserved: got __`. Repo Repro recorded
`DIFFERENTIAL_VALIDATED` using Go 1.27.4 on Linux amd64 with a caller-supplied test.

## Inspect the evidence without Docker

Download `xstrings-64-diagnostics.zip` from the [v0.2.0 release](https://github.com/NovolineGast/RepoRepro/releases/tag/v0.2.0).
With Repo Repro 0.2.0 installed, run these commands in the download directory.

```bash
repro capsule inspect xstrings-64-diagnostics.zip
repro capsule unpack xstrings-64-diagnostics.zip ./case-evidence
```

Open `case-evidence/report.md` for the verdict, `tests/test_reproduction.go` for the test, or
`reference/result.json` for each attempt. The diagnostic download contains evidence only.

## Reproduce and replay with Docker

Follow the [Go historical case walkthrough](../go.md#historical-public-case) to prepare the reviewed
runtime image, seal the supplied test, and capture your own replayable Capsule.
This path requires Linux amd64 Docker with cgroup v2 and works with a caller-supplied test.
After replay, open `replay-report.md` in the replay workspace for the new verdict.
The upstream source and fix belong to the [xstrings project](https://github.com/huandu/xstrings).
