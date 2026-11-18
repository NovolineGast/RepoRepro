"""Trusted Go launcher; source code only runs inside the existing supervised sandbox."""

LAUNCHER = r"""
import json, os, pathlib, subprocess, sys
root = pathlib.Path('/workspace')
for name in ('.repo-repro-go-cache', '.repo-repro-go-tmp'):
    (root/name).mkdir(exist_ok=False)
env = {
    'PATH': '/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin', 'HOME': '/tmp/repro-home',
    'TMPDIR': '/workspace/.repo-repro-go-tmp', 'GOTMPDIR': '/workspace/.repo-repro-go-tmp',
    'GOCACHE': '/workspace/.repo-repro-go-cache', 'GOMODCACHE': '/opt/go-mod',
    'GOPATH': '/tmp/repro-home/go', 'GOPROXY': 'off', 'GOSUMDB': 'off',
    'GOTOOLCHAIN': 'local', 'GOWORK': 'off', 'GOENV': 'off', 'GOFLAGS': '-mod=readonly',
    'CGO_ENABLED': '0', 'GOMAXPROCS': '2', 'GOTELEMETRY': 'off', 'TZ': 'UTC', 'LANG': 'C.UTF-8',
}
argv = ['/usr/local/go/bin/go', 'test', '-json', '-count=1', '-p=1', '-parallel=1',
        '-timeout=30s', '-run=^TestRepro$', './repo_repro_generated']
events = []
truncated = False
size = 0
with subprocess.Popen(argv, cwd=root, env=env, stdout=subprocess.PIPE,
                      stderr=None, stdin=subprocess.DEVNULL) as process:
    for raw in process.stdout:
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
        size += len(raw)
        if size > 200000 or len(events) >= 2000:
            truncated = True
            continue
        try:
            events.append(json.loads(raw))
        except (ValueError, UnicodeError):
            truncated = True
    code = process.wait()
report = {'schema_version': '2.0', 'framework': 'go-test', 'events': events,
          'exit_code': code, 'truncated': truncated}
pathlib.Path('/tmp/repo-repro-go.json').write_text(json.dumps(report))
sys.exit(code)
"""
