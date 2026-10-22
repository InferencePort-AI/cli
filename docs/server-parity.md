# Desktop hosting protocol audit

Reference: sharktide/inferenceport-ai f584e76fa74d0f4764a056d659accee2e1b7a517.
Inspected server.ts, ecdhAesSession.ts, proxy52458Client.ts, sysinfo.ts,
auth.ts (session restoration, refresh and token issuance), orgPolicy.ts hosting
caller and filtering, and CLI proxy/auth/config/policy/server commands and tests.

## Routing order and endpoint matrix

| Method/path | Authentication | Result/limits |
|---|---|---|
| OPTIONS, any path | none | 204 for two allowed origins, otherwise empty 403 |
| GET /__health (no query) | none | empty 204; shared per-IP limiter |
| GET /__inferenceport/crypto-capabilities (query allowed) | optional bearer | always 200; supported only when verifier returns 200, found and email; configured API keys never advertise support |
| POST /__inferenceport/crypto-handshake | API key or verified allowed user; session validation takes precedence | P-256 handshake; exempt from local rate limit |
| POST /sysinfo (query allowed) | allowed identity | hardware rating; shared limiter; missing size 400, parse/rating failure 520 |
| all remaining methods/paths | allowed identity | Ollama proxy; shared limiter; member regex restriction |

CORS origins are literally http://localhost:52458 and https://inference.js.org,
independent of bound port. Other origins can make ordinary requests but receive
no CORS headers. Methods/header values are copied literally from server.ts.
Bearer prefix is case-sensitive; token is trimmed. Email matching is exact and
first match wins. API key identity is first admin (case-insensitive role), else
first user, else __server_api_key__/admin. Every session request re-verifies its
bearer and exact email against the session and current allowlist. Verifier POST
body is {token,emails}; only Content-Type and optional host-session Authorization
are supplied. No publishable-key fallback or apikey header; no verifier timeout.
429 preserves string error/notice and non-null usage; other failures become 401.

Members (any non-admin role) are blocked only if method is not GET and the raw
URL, including query, matches /pull|rm|delete|create|models|run/i. HEAD is subject
to this rule. This is not a complete read-only policy.

## Cryptography

prime256v1 (P-256), SEC1 point public keys, uncompressed server public key.
ECDH shared secret is 32 bytes, including leading zeroes. SHA256 hashes, in order,
shared secret, UTF-8 challenge string, decoded server nonce, decoded client nonce.
Challenge is 32 random bytes encoded base64url without padding, nonce 16 bytes,
session ID 24 bytes. Client nonce need only be a string; Node base64 decoding is
permissive. There is no challenge-response round trip or replay counter.
AES-256-GCM request envelope: {type:"enc",iv,tag,data}; base64url fields; client
uses 12-byte IV and 16-byte tag. Responses and bearer tokens are NOT encrypted.
Sessions expire at creation + 3600000 ms, with <= boundary and lazy purge on lookup.
Malformed/unverifiable envelopes are forwarded unchanged by the reference.

## Limits, forwarding and logging

One fixed per-IP window shared by health, sysinfo and authenticated forwarding:
30 accepted requests/60000 ms; reset only when now > resetAt. Failed auth,
capabilities, preflight and handshake do not consume it. Bodies buffered up to
20971520 bytes; larger bodies destroy the socket. No inference timeout or
concurrency cap. Forwarded headers exclude host, authorization, connection,
content-length, transfer-encoding, upgrade, proxy-authorization,
proxy-authenticate, te, trailers. Other headers (including session) are retained.
Upstream failures: 500 {"error":"Proxy error"}. Response headers/status stream
through. Absolute http(s) URLs are rejected after auth/limit checks.

Log: InferencePort-Server.log, truncated at start, rotated before writes at >=
10485760 bytes to InferencePort-Server-<epoch-ms>.log. Lines use UTC ISO milliseconds,
[LEVEL], CR/LF/TAB replacement and ANSI color removal. IP SHA256 first 8 hex;
token first 6 chars followed by stars. Connection logging includes raw query.

## Reference defects and explicit security deviations

The reference resolves network-path URLs (//host/path) against Ollama, permitting
SSRF. Uppercase HTTP schemes also evade its case-sensitive absolute-URL guard.
Python rejects network-path targets, absolute schemes and resolved cross-origin
WHATWG targets (including mixed slash/backslash paths) rather than reproduce
these flaws.
The reference's malformed-envelope fallback permits plaintext requests within a
session; preserve wire compatibility, but never interpret failed ciphertext as
successfully authenticated plaintext. There is no transport confidentiality for
tokens/responses or replay prevention; use HTTPS. Log queries can contain secrets;
clients must not put credentials in URLs. No blanket claim of exact parity is
justified until the differential and real-client suites pass; known differences
and platform inspection limitations must remain visible in this document.

## Validation evidence and remaining completion gates

The differential harness loads the real, unchanged reference .ts files using
TypeScript transpilation. Environment adapters provide an Electron data folder,
host session, deterministic hardware snapshot, and local mock network services.
Server routing, cryptography, permissions, scoring, and the real secure-fetch
client are not replaced. Only Date is excluded from HTTP comparisons; response
framing and connection headers are compared. Random handshake values are checked
by deriving the negotiated key and successfully decrypting inference requests.
The crypto-vector test compares fixed P-256 private scalars, public point, shared
secret, SHA256-derived key, ciphertext/tag and plaintext against Node crypto.

Run on Windows PowerShell from the CLI repository:

```powershell
$env:PYTHONPATH="$PWD\src;$PWD\tests"
$env:IPX_DESKTOP_SOURCE="A:\inferenceport-ai\src"
python -m pytest tests --import-mode=importlib -q
python -m ruff check src tests packaging
```

The extra test-helper path/import mode avoids this workstation's unrelated
installed `tests` package. Without desktop sources and TypeScript dependencies,
differential tests explicitly skip; ordinary Python tests still run. CI includes
a dedicated three-platform job pinned to the audited desktop revision. Production
never imports the test loader or launches Node.

Implemented and exercised: every internal route, authentication failures and
quota payloads, role/method/query rules, exact email matching, real handshakes,
AES authentication failure fallback, concurrent session allocation, cross-user
isolation, accepted replay, streaming delivery before completion, disconnect
recovery, chunked requests, 20 MiB boundary and socket destruction, shared rate
window and expiry equality against both processes with a controlled clock, upstream request headers, log truncation/rotation,
malformed sysinfo, hardware warnings/tiers, and real secure-client interoperability.

### Requirements explicitly incomplete (do not advertise exact 1:1 parity)

* Forwarding uses a new Python HTTPConnection per request rather than Node's
  global connection pool; duplicate response headers other than Set-Cookie are currently collapsed.
  Upstream truncation and every possible chunk/trailer boundary need further
  protocol-level tests. This is not byte-for-byte TCP packet equivalence.
* cryptography's GCM adapter supports nonces of 8â€“128 bytes; the actual desktop
  client always uses 12. Exotic otherwise-valid Node GCM nonces outside that
  range are not reproduced. Standard client ciphertexts interoperate.
* CLI organization policy is checked at startup and refreshed on a five-minute
  watcher that revokes hosting/membership changes. Existing CLI policy-cache
  fallback behavior is retained and has not been proven identical to desktop
  offline/cache semantics.
* The real Node desktop secure-fetch function has passed against Python with
  mock token verification and Ollama. A packaged Electron UI session with live
  verification and real-model inference is not exercised; credentials, user
  interaction and a loaded real model were not supplied for that test.
* Differential CI results on Linux/macOS, detached lifecycle smoke tests on all
  platforms, and exhaustive parser/logging comparisons remain completion gates.
  Console output still follows CLI --verbose instead of desktop's unconditional
  console logging. The on-disk structure and rotation thresholds are covered.

These limitations are deliberate disclosures, not waived acceptance criteria.
The pull request should remain a draft until they are resolved or explicitly
accepted. The network-path SSRF rejection is an intentional security deviation,
not an unexplained mismatch. No desktop production code is modified.


## Local validation results

Windows, Python 3.12, Node 24.11.0:

* Full suite: `python -m pytest tests --import-mode=importlib -o addopts='' -q --disable-warnings` — **405 passed, 36 warnings**, 109.96 s. Warnings come from realtime test-server thread cancellation.
* `python -m ruff check src tests packaging` — passed.
* `python -m compileall -q src/ipx/proxy.py src/ipx/server_crypto.py src/ipx/server_hardware.py src/ipx/commands/server.py` — passed.
* `python packaging/check_versions.py` — all versions agree.
* Real desktop secure-fetch handshake/encrypted request and actual detached CLI start/status/stop — passed as part of the suite.
* Real local hardware-rating smoke test — returned measured CPU/RAM/GPU detection and a rating.
* Desktop source working tree — unchanged.

Cross-platform CI has been configured; results are not claimed before it runs.


## Follow-up: native measurement and HTTP parsing

The original two hardware/parser gaps now have native implementations and
cross-platform validation, rather than waived acceptance criteria.

CPU inputs use Windows CIM plus registry FeatureSet, Linux lscpu/procfs and macOS
sysctl. Brand normalization, logical core counts and cache byte units follow
systeminformation 5.33.x. Memory uses Linux MemAvailable, native Windows physical
memory and macOS total minus anonymous-minus-purgeable-plus-wired-plus-compressed pages,
using the OS page size (16 KiB on Apple Silicon). The local live comparison matched CPU brand, cores, L3 cache, flags and
total RAM exactly. Available-memory samples are sequential and can change.

Windows GPU capacity first reads 64-bit HardwareInformation.qwMemorySize by PCI
identifier and falls back to native DXGI SIZE_T capacity. It no longer relies on
32-bit AdapterRAM truncation. NVIDIA NVML queries total/free memory directly from
the driver, without requiring nvidia-smi. Linux AMD DRM mem_info_vram_total/used
provides actual adapter-wide free VRAM; PCI enumeration retains adapters without
DRM nodes. macOS parses the same system_profiler XML records and GB/MB shared
VRAM fields. Apple unified RAM is not counted twice as dedicated VRAM; absent
VRAM measurements remain null. Failed optional driver probes retain successful
OS results. DXGI per-process budgets are NOT labeled as adapter free memory.

Native accuracy can exceed desktop fallback accuracy: DXGI capacity overrides
ambiguous/truncated WMI AdapterRAM, and AMD DRM supplies a free-memory hint where
the desktop lacks one. These are explicit measurement improvements, while the
rating algorithm and output schema are unchanged. The Windows FeatureSet flags
retain the reference algorithm, including its omission of CPUID leaf 7 AVX2 and
AVX512, so the score is not silently changed.

Set IPX_OLLAMA_VENDOR_ROOT to the exact desktop vendor/electron-ollama directory
to inspect the same lib/ollama build. That explicit root takes precedence over
standalone installs. Otherwise the CLI inspects the Ollama installation it
actually serves. ROCm-before-CUDA precedence and macOS metal match the reference.

The server no longer imports BaseHTTPRequestHandler or HTTPServer. Native
httptools/llhttp validates HTTP framing, and native ada-url implements WHATWG URL
resolution, including percent-encoded dot segments. No Node/Electron runtime is
required. The transport implements first-value singleton headers (including
Authorization), joined duplicate custom headers/cookies, CL/TE rejection, chunk
validation, CONNECT/upgrade handling, HTTP/1.0 framing, required Host, unread-body
draining, parser error responses, 16 KiB header limits and the 1000-field (2000 raw name/value pairs) overflow rejection.
Node defaults are reproduced: 60-second header receipt, 300-second total request
receipt, 30-second deadline scans, and 5-second keep-alive plus the 1-second socket
buffer. Completed request bodies clear receipt deadlines, so long inference is
not timed out. Scaled deadline tests run the same inputs against real Node
server options; there are no production test bypasses.

Raw-wire tests cover malformed methods/versions/headers, duplicate lengths,
CL+TE, unsupported coding, invalid chunks, missing Host, HTTP/1.0, CONNECT and
upgrade, duplicate Authorization, header-size boundaries and encoded dot paths.
Native fixtures cover >4 GiB Windows capacity, DXGI fallback, AMD free VRAM,
Apple unified memory, native page size and NVML outages. Live native collectors
are compared with real systeminformation in Windows/Linux/macOS differential CI.

The original Linux client-test failure came from a fixed-port collision. The
unchanged desktop secure client still uses logical port 52458; its test network
adapter maps to an ephemeral listener. Detached startup now avoids HTTPServer's
synchronous reverse-DNS lookup, publishes the PID atomically and records useful
process/server diagnostics on startup failure. macOS tests remain enabled.

Local follow-up server/hardware suite: 150 passed. Updated CI results are tracked
on the pull request; they are not claimed before completion.


### Reference runtime pin

Differential CI pins Node 24.21.0. Its security hardening rejects requests over
1000 header fields with 431; Node 24.11 silently discarded excess fields instead.
Using a floating Node 24 selector had selected different cached minor versions
on different runner OSes, exposing this runtime difference. Python uses the
hardened rejection and separately enforces trailer limits. Local tests can select
the same isolated reference binary with IPX_REFERENCE_NODE. This changes no
desktop application source. Tests against older runtimes may correctly expose
this security-driven compatibility difference rather than silently masking it.


Live memory validation brackets an isolated systeminformation memory query with
native samples while the same Node observer remains resident throughout. Native
probes do not load the test-only TypeScript compiler or system_profiler. This
removes the observer process startup/exit footprint from the comparison. The
reference must lie within the observed native before/after range plus a 64 MiB
command-scheduling margin; static totals and OS accounting fixtures still compare
exactly. Native measurements and application rating behavior are not changed.


### Exact dependency audit correction

The desktop lockfile pins systeminformation 5.33.15, but the local desktop
node_modules had stale 5.31.7 files. The locked package was fetched separately and
its SHA-512 verified against package-lock.json. The relevant CPU flags, brand,
cache and controller algorithms were re-audited against those exact files.
The substantive memory change is macOS: 5.33.15 counts anonymous - purgeable +
wired + compressed pages as active/unreclaimable. Python now uses that exact
formula; the fixture includes all four counters and deliberately different
Pages-active values to catch regression to the stale algorithm. Local reference
probes use the verified locked package via IPX_SYSTEMINFORMATION_PATH, and CI
already installs that exact dependency version.
