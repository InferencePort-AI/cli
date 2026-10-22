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
Python rejects network-path targets and all absolute schemes rather than reproduce
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

* Native hardware collection is platform-adapted, not a reimplementation of
  systeminformation. Win32 AdapterRAM may truncate large VRAM capacities without
  nvidia-smi; Linux lspci does not supply AMD free VRAM; macOS shared-memory GPUs
  have no dedicated VRAM measurement. CPU brand/cache/flag discovery and memory
  accounting may differ. Scoring with identical measured snapshots is compared,
  but native collectors have not been validated on Linux/macOS hardware here.
  Ollama acceleration libraries are inspected in the CLI's standalone install,
  rather than the desktop's bundled vendor directory.
* HTTP parsing uses BaseHTTPRequestHandler, not llhttp. Malformed/duplicate
  headers, unsupported transfer codings, CONNECT/upgrade, HTTP/1.0, ambiguous
  framing, percent-encoded dot segments, request/header deadlines and oversized
  headers are not proven equivalent. Node's default request/header timeout
  behavior has not been fully ported. Keep-alive idle expiration is implemented.
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
