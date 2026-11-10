# Security maintenance

Run advisory scans with the interpreter from the installation being checked:

```sh
.venv/bin/python scripts/security_audit.py --output /tmp/yurios-advisories
```

This network check audits every version in the cross-platform constraints,
including alternate Python-version pins, every installed Python distribution,
and both npm lockfiles. It runs pip-audit through an isolated uv tool environment.
It returns nonzero for findings/errors and writes inventories, raw JSON reports
and exit statuses. Review skipped packages too: a private wheel or spaCy model
absent from PyPI cannot be assessed there. Scan separately installed optional
backends with their own interpreter. Native OS/driver/Qt/Cubism components also
need their vendor advisories; npm/PyPI results do not cover them comprehensively.
The offline `scripts/check.sh` gate deliberately does not depend on advisory servers.

## November 2026 remediation

The Python floors and regenerated constraints update certifi, Pillow, pypdf, MCP
(on its 1.x line), LiteLLM, aiohttp, PyJWT, filelock, multidict, urllib3, setuptools
and accelerate. Web tooling and Electron/undici are updated in their lockfiles.
Restart running clients after installation to replace cached TLS contexts.
The pin generator leaves CUDA runtime packages unpinned with the rest of the
machine's torch stack, so a dependency refresh preserves the chosen hardware build.

Night-job ZIP imports now enforce compressed, expanded, aggregate and file-count
limits before writes. Image and PDF parsers run in disposable processes with
resource budgets; format allowlists precede image decoding. This contains resource
exhaustion, and is not a native-code security sandbox. Parser limits were
verified on Linux; macOS/Windows packaging needs validation on those systems.
Electron grants only the selected origin's main-frame microphone, denying camera, display capture, popups
and foreign navigation. Linux tests cover permission decisions; an actual Windows
Electron capture/session smoke test remains necessary when packaging the shell.

Live2D runtime downloads have reviewed SHA-256 checks and browser SRI. If the
mutable Cubism URL changes, the download fails and preserves the previous file;
review the new upstream version before updating the digest and SRI together.
Bundled embedder and forge companion repositories use immutable revisions.
Custom models and checkpoints remain an explicit trust decision by the owner.
Main HTML pages send CSP, disallow framing and MIME sniffing; Live2D permits eval
for Pixi 6. Keep script policy changes covered by a real browser check.

## Unpatched optional dependency

`llama-cpp-python` still requires `diskcache==5.6.3`. Its pickle deserialization
[CVE-2025-69872 / GHSA-w8v5-vhqr-4h9v](https://github.com/advisories/GHSA-w8v5-vhqr-4h9v)
has no fixed upstream release at this writing. YuriOS never constructs a
DiskCache or enables llama.cpp's disk cache; GGUF metadata is JSON and model state
stays in memory. The package is installed transitively by the optional `llm`
extra. LiteLLM's updated base dependency graph no longer requires it. Do not share
writable cache directories with untrusted users, and do not enable a pickle-backed
cache through extensions. This finding remains visible in advisory scans rather
than being suppressed. Remove the `llm` extra from deployments that do not need
direct GGUF inference, or wait for a patched upstream dependency before claiming a
clean Python advisory report.

## Remote access

Keep loopback binding for local-only use. For LAN or Internet access, put YuriOS
behind an HTTPS reverse proxy, retain owner authentication, forward WebSocket/SSE
connections, and restrict direct access to the backend port. Pair/login over the
HTTPS URL so tokens and cookies are not sent over cleartext HTTP. The local daemon
does not provision certificates or change the host's proxy/firewall configuration.

## Verified results — 2026-11-10

The full release gate passed: lint, typechecking (228 source files), 2,632 Python
tests (4 skipped), 284 frontend tests, desktop permission tests, the Python 3.11
wheel/install/resolve smoke test, and cached plus cold-download embedder checks.
The web production build also passed. Real browser checks covered text, Live2D,
3D and settings with no console errors under CSP. The isolated ZIP reproduction
changed from HTTP 200 with an 8 MiB write to HTTP 413 with zero bytes written.

Live advisory scans covered 165 conditional constraint versions, 190 installed
Python distributions, and both npm lockfiles. The npm graphs had zero findings;
Python retained only the diskcache advisory above (the audit feed repeats its ID).
The installed spaCy model `en-core-web-sm==3.8.0` was explicitly skipped because it
is absent from PyPI. The installed dependency graph passed `uv pip check`.
