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
ipx featherless deploy Qwen/Qwen2.5-7B-Instruct   # personal, or for the org with `-O acme`
ipx agent chat acme-bot/support      # managed agents
ipx mcp council reason "…" --tier basic   # Council of LLMs MCP

ipx model pull llama3.2              # local models (same engine as the desktop app)
ipx local chat llama3.2
ipx server start --allow teammate@acme.com --detach     # host your models for others
ipx import space black-forest-labs/FLUX.1-schnell       # add to the desktop app

ipx shield scan "Ignore previous instructions…"     # AI Shield (docs: readthedocs, "AI Shield")

ipx usage get                        # quotas, wallet and org spend
```

Every command has `--help`, and every command accepts the global `--json` flag for scripting:
`ipx --json usage get | jq .quota`.

## Command map

| Group | What it does |
| --- | --- |
| `ipx auth` | `login`, `logout`/`signout`, `status`/`whoami`, `context <org>`, `token` |
| `ipx org` | members, invitations, teams, API keys, billing, usage, audit, domains, SSO, policy, desktop policy |
| `ipx p2g` | credit-billed: `chat`, `text`, `image`, `video`, `audio …`, `3d`, `models`, `balance`, `ledger`, `keys`, `deploy` |
| `ipx featherless` | `models`, `status`, `list`, `get`, `deploy`, `cancel`, `restore`, `terminate`, `usage` — personal deployments, or the organization's with an org context |
| `ipx agent` | `list`, `registry`, `get`, `create`, `update`, `delete`, `chat`, `ask`, `profile`, `settings`, `invite …`, `members …` |
| `ipx mcp council` | `tiers`, `models`, `reason`, `tools`, `test`, `connect <client>` — the hosted Council of LLMs MCP server |
| `ipx gen` | subscription-billed (Lightning): `chat`, `text`, `image`, `video`, `audio …`, `models`, `plan` |
| `ipx shield` | AI Shield detection and risk scoring: `scan`, `analyze`, `phishing`, `url`, `check`, `disposable`, `request`, `file`, `password`, `listing`, `review`, `domain`, `tasks` — see the [AI Shield docs](https://inferenceport-ai.readthedocs.io/en/latest/cli/shield.html) |
| `ipx model` / `ipx local` | manage local models / chat with them |
| `ipx server` | `start`, `stop`, `status`, `logs` — host your local models on port 52458 |
| `ipx import` | `space`, `website`, `file`, `list`, `rm` for the desktop app library |
| `ipx usage` | `get`, `tiers` |
| `ipx config`, `doctor`, `status`, `api`, `rewards`, `completion` | tools |

Full documentation lives with the desktop app docs: **<https://inferenceport-ai.readthedocs.io>** (CLI section, and AI Shield).

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
