# AI investigation

`repro investigate` combines model-assisted diagnosis and file selection with a bounded test revision loop. It is available from v0.2.0.

The model interprets the Issue, selects relevant files from a pinned Git catalog, and writes a reproduction candidate for Python or Go. Local validation checks the plan, citations, test shape, and repository calls. Accepted candidates run repeatedly in fresh containers. Validation errors and eligible original-version execution feedback can inform a replacement candidate.

The caller supplies the failure oracle and runtime. The model cannot change them. Once a candidate reproduces the target failure, it is frozen. Optional original/fixed verification runs independently; fixed-version source and execution feedback are not supplied to the model for tuning the test.

## Run the owned Unicode demo

Prepare the images described in [examples/README.md](../examples/README.md#prepare-test-images). Set `OPENAI_API_KEY` securely in the calling shell; do not put a key in source files or command-line arguments.

Bash, with a fresh demo directory:

```bash
IMAGE_ID=$(python -c "import json; print(json.load(open('.tools/demo-images.json'))['pytest_image'])")
python examples/create_ai_demo.py .repo-repro/ai-demo --image "$IMAGE_ID"
ORIGINAL=$(python -c "import json; print(json.load(open('.repo-repro/ai-demo/commits.json'))['original'])")
FIXED=$(python -c "import json; print(json.load(open('.repo-repro/ai-demo/commits.json'))['fixed'])")
repro investigate .repo-repro/ai-demo/repository .repo-repro/ai-demo/issue.json \
  .repo-repro/ai-demo/oracle.json .repo-repro/ai-demo/environment.json \
  --model deepseek-flash --base-url https://api.deepseek.com \
  --legacy-max-tokens --reasoning-effort none \
  --ref "$ORIGINAL" --fixed-commit "$FIXED" \
  --max-plan-attempts 2 --max-candidates 3 --max-model-tokens 4096 \
  --artifacts .repo-repro/ai-demo/evidence --output .repo-repro/ai-demo/session.json \
  --result-output .repo-repro/ai-demo/result.json
```

`--legacy-max-tokens` selects `max_tokens`. `--reasoning-effort none` disables thinking for this example's output budget, as supported by [DeepSeek's API](https://api-docs.deepseek.com/api/create-chat-completion/). Other providers may need different settings; the default endpoint remains OpenAI-compatible Chat Completions.

To let the model improve an existing weak test, add:

```text
--initial-test .repo-repro/ai-demo/seed_test.py
```

The supplied seed checks ASCII input and deliberately does not reproduce the Unicode bug. Its actual passing result gives the model a useful revision signal. The model must choose a triggering input and preserve the original behavior contract.

In PowerShell, read the image/commit JSON with `Get-Content -Raw ... | ConvertFrom-Json`, then pass the resulting strings to the same CLI options.

## Budgets and stopping

- Planning: 1–2 requests, default 2. Only locally rejected plans are eligible for a correction request. Provider/network errors stop immediately.
- Candidates: 1–5 attempts, default 3. Rejected candidates consume an attempt. An initial user test consumes an attempt without a model generation call.
- Default maximum: five model calls, each with an explicit output-token budget. Model requests and individual sandbox attempts also retain their existing time/size/resource limits.
- Revisions use only original-version observations. Environment errors, timeout, flaky outcomes, incomplete evidence, or uncertain cleanup stop investigation. Equivalent candidate ASTs are not executed again.
- A successful original reproduction ends candidate search. A failed fixed-version verification is reported without feeding that result back to the model.

This is bounded search over distinct candidate tests, not repeated sampling of one test until it happens to fail. Each accepted candidate must still satisfy the configured repetitions and independent evidence rules.

## Outputs

`session.json` contains the AI plan, actual call count, budgets, candidate/rejection history, the last executed result, optional differential verification, and a stop reason. The plan is marked `ai_inference`; it does not replace static or reporter facts.

`--result-output` writes the ordinary reproduction/differential result for existing Capsule commands:

```bash
repro capsule export .repo-repro/ai-demo/result.json .repo-repro/ai-demo/case.zip \
  --artifacts .repo-repro/ai-demo/evidence
```

If no candidate executes, no result file is written. Use fresh output directories and check the exit status and session. A single-version `reproduced` stop reason does not mean a fix has been verified.

Model requests/responses, rejected plans, candidate generation, and redacted feedback are linked in the private artifact store. Planning sends the Issue, a filtered file catalog, and bounded seed code. Generation sends selected context; revisions also send redacted original logs/structured evidence. Review the data and provider before investigating private repositories.

## Validation scope

On 2026-11-16, the real `deepseek-flash` endpoint was exercised on the owned Unicode normalization fixture in two scenarios: generation from Issue context and revision of the weak ASCII seed. Both ended in independent `DIFFERENTIAL_VALIDATED` results. The successful scenarios used four and two model calls respectively; local checks rejected malformed intermediate replies rather than accepting them. Earlier failed development trials were retained locally.

These are functional acceptance cases, not a benchmark or a success-rate estimate for real-world repositories. Automated CI uses controlled model responses, with real Docker coverage for the feedback and verification path. Live paid calls are not part of CI.
