# Capsule v1

Capsules are ZIP archives with one `reproduction/` root. Format version `1.0.0` is independent of the package version. The reader accepts compatible `1.0.x` versions and rejects unknown major/minor versions.

`repro.yaml` uses the UTF-8 JSON subset of YAML 1.2. Duplicate keys, non-finite numbers, unknown fields, and inconsistent references are rejected. The full contract is [capsule-v1.schema.json](capsule-v1.schema.json).

## Contents

```text
reproduction/
  repro.yaml
  checksums.sha256
  environment.json
  sources/original.tar
  sources/fixed.tar
  images/runtime.tar
  tests/test_reproduction.py
  evidence/stdout.log
  evidence/stderr.log
  evidence/trace.json
  reference/result.json
  README.md
  report.md
  Dockerfile
  reproduce.sh
```

`offline` mode contains captured sources, the frozen image, sealed test, configuration, and diagnostics. `sources/fixed.tar` exists only for differential results. `diagnostic` mode contains reference materials and cannot be replayed.

Each payload entry has a role, size, and SHA-256. The checksum file covers the manifest and payload, excluding itself. Hashes detect changes; they do not authenticate Git provenance or the publisher.

## Import and replay

Validation checks the complete ZIP/catalog/schema/checksums before extraction. It rejects traversal, absolute/platform-ambiguous paths, duplicates, links, devices, encrypted members, unsupported compression, suspicious ratios, and oversized inputs. Extraction stages data and publishes it into a new destination only after validation. Source tar files use a separate bounded regular-file reader.

Reading, validating, and extracting never run bundled code or invoke Docker. `replay` is explicit and requires a local policy. It loads the frozen image, uses the local trusted launcher/collector, and runs the archived test/source in new containers. Bundled `Dockerfile` and `reproduce.sh` are diagnostic data. Incompatible local helper hashes are rejected.

```bash
repro capsule schema
repro capsule inspect .repo-repro/demo/case.zip
repro capsule inspect .repo-repro/demo/case.zip --summary
repro capsule unpack .repo-repro/demo/case.zip .repo-repro/capsule-inspection
repro capsule replay .repo-repro/demo/case.zip .repo-repro/capsule-replay \
  --policy .repo-repro/demo/policy.json
```

Use `inspect --summary` for the schema version, mode, language, framework,
runtime, recorded outcome and pinned commits. Plain `inspect` and `validate`
print the full validated manifest as JSON. Both inspection formats validate
the archive before printing any manifest details.

Run the [demo](../examples/README.md) first; extraction and replay destinations must be new. Export takes the recorded result and original artifact store, not the current working tree. `--diagnostic-only` does not capture an image and produces a non-replayable archive.

Replay writes `replay-report.json`, `replay-report.md`, and an independent artifact store. Status is `MATCHED`, `DRIFTED`, `INCONCLUSIVE`, or `ENVIRONMENT_ERROR`, based on new behavior and runtime evidence rather than the historical log text.

## Limits and secrets

Default ZIP limits are 10,000 entries, 2 GiB per file, 4 GiB expanded total, 8 MiB metadata, and 32 MiB source slots. Export uses stored ZIP members; frozen images can make archives large. Diagnostic logs have a separate 64 MiB export budget.

Diagnostics are redacted. Detected credentials in replay-critical data cause rejection because silently editing code or image data would change the experiment. Image scanning covers raw/gzip layers and private-key patterns in all files; some system libraries contain public self-test private keys and are also rejected. The example fixture-image helper explicitly omits GnuTLS for this reason.

Scanning cannot identify every secret, and a Capsule is not a trust certificate. Loading an image is part of the execution boundary. Review the archive and use a disposable VM for high-risk inputs. See [SECURITY.md](../SECURITY.md).
