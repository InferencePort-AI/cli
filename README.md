<p align="center">
  <img src="https://raw.githubusercontent.com/sharktide/inferenceport-ai/main/src/build/icon.png" width="84" alt="InferencePort AI">
</p>

<h1 align="center">ipx</h1>
<p align="center"><b>The InferencePort AI command line.</b><br>
Cloud chat &amp; media generation · organizations · local models · host server — one native binary.</p>

```text
  ██╗██████╗ ██╗  ██╗
  ██║██╔══██╗╚██╗██╔╝
  ██║██████╔╝ ╚███╔╝
  ██║██╔═══╝  ██╔██╗
  ██║██║     ██╔╝ ██╗
  ╚═╝╚═╝     ╚═╝  ╚═╝
```

## Install

`ipx` ships as a self-contained native binary (compiled with [Nuitka](https://nuitka.net)). Use whichever package
manager you already have — none of them need Python or anything else installed beyond themselves.

| You have | Command |
| --- | --- |
| **pip** | `pip install inferenceport-cli` |
| **npm** | `npm install -g @inferenceport/ipx` |
| **cargo** | `cargo install ipx-cli` |
| macOS / Linux shell | `curl -fsSL https://raw.githubusercontent.com/inferenceport-ai/cli/main/install.sh \| sh` |
| Windows PowerShell | `irm https://raw.githubusercontent.com/inferenceport-ai/cli/main/install.ps1 \| iex` |
| Nothing | Download from [Releases](https://github.com/inferenceport-ai/cli/releases) |

## Tour

```bash
ipx auth login                       # email, GitHub, Google, Microsoft, Hugging Face, SSO or API key
ipx auth context acme                # act as an organization (billing, policy, `ipx org …`)

ipx gen chat                         # interactive chat with your Lightning subscription
ipx gen image "a lighthouse at dawn" -o lighthouse.jpg
ipx gen video "a drone shot over a canyon" --seconds 8
ipx gen audio speech "Welcome to InferencePort"

ipx p2g chat                         # credit-metered Pay-2-Go API
ipx p2g 3d --image chair.jpg -o chair.glb
ipx p2g balance

ipx org members list                 # organizations
ipx org invite create dev@acme.com --role admin
ipx org keys create ci --monthly-limit 25

ipx model pull llama3.2              # local models (same engine as the desktop app)
ipx local chat llama3.2
ipx server start --allow teammate@acme.com --detach     # host your models for others
ipx import space black-forest-labs/FLUX.1-schnell       # add to the desktop app

ipx shield scan "Ignore previous instructions…"          # AI Shield: prompt attacks, abuse, secrets, PII
cat page.html | ipx shield scan --task agent_tool_output --fail-on review
ipx shield analyze --task signup --email new@mailinator.com --ip 203.0.113.7
ipx shield phishing -f suspicious.eml

ipx usage get                        # quotas, wallet and org spend
```

## AI Shield in scripts and CI

`ipx shield` sends text, identities or messages to InferencePort AI Shield and prints a verdict: a 0–100 risk score,
a decision (`allow` · `challenge` · `rate_limit` · `review` · `block`), the reasons and suggested mitigations.

```bash
ipx shield tasks                                             # task profiles + thresholds (no sign-in)
ipx shield scan -f prompt.txt --task prompt_guard --fail-on review   # exit 3 on review/block
git diff | ipx shield scan --task dlp --fail-on review              # catch leaked keys before you push
ipx -q shield scan -f transcript.txt --task dlp --redact > clean.txt # redacted copy only
ipx shield analyze --task login --email bob@acme.com -m failed_logins=30 -m user_agent=python-requests/2.31
ipx shield url bit.ly/3abc https://paypa1.com/login --offline
```

Tasks: `general`, `signup`, `login`, `payment`, `content_moderation`, `prompt_guard`, `agent_tool_output`, `phishing`,
`dlp`. `--offline` skips the LLM reviewer and third-party lookups; `--feature NAME=on|off`, `--trusted-domain` and
`--challenge-at/--review-at/--block-at` tune the pipeline.

Every command has `--help`, and every command accepts the global `--json` flag for scripting:
`ipx --json usage get | jq .quota`.

## Command map

| Group | What it does |
| --- | --- |
| `ipx auth` | `login`, `logout`/`signout`, `status`/`whoami`, `context <org>`, `token` |
| `ipx org` | members, invitations, teams, API keys, billing, usage, audit, domains, SSO, policy, desktop policy |
| `ipx p2g` | credit-billed: `chat`, `text`, `image`, `video`, `audio …`, `3d`, `models`, `balance`, `ledger`, `keys`, `deploy` |
| `ipx gen` | subscription-billed (Lightning): `chat`, `text`, `image`, `video`, `audio …`, `models`, `plan` |
| `ipx shield` | AI Shield: `scan` (text/files/stdin, `--redact`), `analyze` (signup/login/payment signals), `phishing` (incl. `.eml`), `url`, `tasks`; `--fail-on <decision>` exits 3 for CI |
| `ipx model` / `ipx local` | manage local models / chat with them |
| `ipx server` | `start`, `stop`, `status`, `logs` — host your local models on port 52458 |
| `ipx import` | `space`, `website`, `file`, `list`, `rm` for the desktop app library |
| `ipx usage` | `get`, `tiers` |
| `ipx config`, `doctor`, `status`, `api`, `rewards`, `completion` | tools |

Full documentation lives with the desktop app docs: **<https://inferenceport-ai.readthedocs.io>** (CLI section).

## Develop

```bash
python -m venv .venv && . .venv/bin/activate      # .venv\Scripts\activate on Windows
pip install -e ".[dev]"
pytest                                            # mocked-API suite, no network or credentials needed
python packaging/build_binary.py                  # Nuitka → dist/ipx-<os>-<arch>
```

Layout: `src/ipx` (the CLI), `packaging/npm` (launcher + platform packages), `packaging/cargo` (Rust launcher that embeds
the release binary), `.github/workflows` (CI and the multi-platform release pipeline).

## License

Apache-2.0 — © InferencePort LLC.
