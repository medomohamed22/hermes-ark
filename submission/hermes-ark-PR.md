## What does this PR do?

Adds **Hermes Ark** to the plugin catalog (`plugin-catalog/hermes-ark.yaml`, one file), pinned to
[`a23e529c1f636aa49d974576cdc5cb15fc8242d3`](https://github.com/medomohamed22/hermes-ark/commit/a23e529c1f636aa49d974576cdc5cb15fc8242d3) (updated after review; first pinned at `f2d53787`).
I am the owner of https://github.com/medomohamed22/hermes-ark.

Hermes Ark is a time machine for Hermes itself: encrypted, versioned backups of memory, SOUL, skills, config,
cron, sessions and every profile, with item-level restore.

- **Automatic:** a snapshot right before every `memory` / `skill_manage` write, plus periodic full snapshots after sessions.
- **Health guard:** when memory or skills suddenly shrink, a note goes into the tool result, so the agent tells the user at once, along with the exact `/ark restore …` command that undoes it.
- **Restore:** everything, one kind (memory / skills / config / cron), one skill, or one profile. A preview comes first and a safety snapshot is taken before every restore. Two-phase: all blobs are verified before the first write.
- **Off-site:** any S3-compatible storage (AWS, R2, B2, MinIO…) or a folder; `hermes ark pull` restores onto a new machine; `hermes ark verify` proves every snapshot restores.
- **Self-defense:** a prompt-injected agent's tool calls that would prune, delete, reconfigure or switch off Ark are refused, and other changes to Ark raise a tamper alarm. Ark never overrides the user: enabling, disabling, settings and `hermes plugins update/remove` always win. This was found by testing with a real model: a hidden comment in a file made the agent run `hermes config set …auto_snapshots false`.
- **Surfaces used:** `tool_execution` middleware, an `on_session_end` hook, the `ark_checkpoint` tool, `ctx.register_cli_command` (`hermes ark …`), `ctx.register_command` (`/ark …`), and a Desktop page (`desktop/plugin.js`, plugin SDK only) with a `dashboard/plugin_api.py` router.

Built for the Hermes Agent Plugin Hackathon.

## Related Issue

None — new catalog entry.

## Type of Change

- [x] ✨ New feature (non-breaking change that adds functionality)

## Changes Made

- `plugin-catalog/hermes-ark.yaml` — new entry (`tier: community`, `category: tools`, `requires_hermes: ">=0.21.6"`, also declared in `plugin.yaml`).

## Disclosure (rule 13)

- **Reads:**
  - The Hermes home and its profiles, to back them up.
  - Hermes credential files (`.env`, `auth.json`, vault, pairing, browser logins, Webapp session token) are **never** read, captured or restored. Restore also skips them in snapshots made by older versions.
  - Ark's own secrets (`ARK_PASSPHRASE`, `ARK_S3_ACCESS_KEY_ID`, `ARK_S3_SECRET_ACCESS_KEY`) come only through `agent.secret_scope.get_secret` and are never stored or logged.
- **Writes:**
  - Snapshots go to the repository folder (default `$HERMES_HOME/plugin-data/hermes-ark/repo`) and Ark's data folder.
  - The Hermes home is written only on a restore the user confirms (terminal, `/ark … yes`, or the Desktop dialog). The agent has no restore tool.
  - Ark never writes `config.yaml`, secrets or its own plugin files. The one repair is its repository key file, after an agent tool call deleted it.
- **Middleware:** `tool_execution` runs on every tool call.
  - It snapshots before memory/skill writes and adds a note when the health guard fires.
  - It refuses calls that would delete, rewrite or reconfigure Ark's backups or settings, or disable Ark, returning a "BLOCKED by Hermes Ark" result. `allow_agent_admin: true` lifts this.
  - Changes that get through raise a tamper alarm only. Hermes' own approvals are never bypassed.
- **Background threads:** periodic snapshot / retention / auto-push after sessions; a watchdog in the agent process that checks Ark every 10 s and **stops when the plugin is disabled or unloaded**.
- **Network:** only to the user-configured S3 endpoint, over HTTPS (plain http only for localhost). Redirects are refused, responses are size-capped, and XML with a DTD is refused.
- **Not included:** no subprocesses, no telemetry, no self-updater, no Python dependencies beyond Hermes' own `cryptography`, no binaries.

## How to Test

1. `hermes plugins install medomohamed22/hermes-ark --enable` (or by catalog name once merged), then restart the gateway.
2. `hermes config set ARK_PASSPHRASE '<a passphrase>'`, then `hermes ark init` and `hermes ark snap first`.
3. In a chat, ask the agent to remove most memory entries. The tool result carries the health-guard note, and the agent relays the `/ark restore memory <id> yes` command. Run it, start a new chat, and the memory is back.
4. `hermes ark status` · `hermes ark list` · `hermes ark verify`, or open the **Ark** page in Hermes Desktop.

Validation at the pinned commit (fresh clone): `hermes plugins validate <clone> --install-deps` → **Validation passed**.
Every check is ✓, including `security scan — safe`, `no core override` and `desktop surface`, with no private-import warnings.
`python scripts/validate_plugin_catalog.py plugin-catalog/` → OK.

## Checklist

### Code

- [x] I've read the [Contributing Guide](https://github.com/NousResearch/hermes-agent/blob/main/CONTRIBUTING.md)
- [x] My commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
- [x] I searched for [existing PRs](https://github.com/NousResearch/hermes-agent/pulls) to make sure this isn't a duplicate
- [x] My PR contains **only** changes related to this feature (one catalog file)
- [x] The plugin's own suite passes at the pinned commit: 174 passed, 1 skipped. The skipped test needs a live S3 server; it was run against one during development. The suite includes adversarial and fuzz tests (path traversal, ciphertext bit-flips, hostile S3 servers, prompt-injection attacks on Ark itself).
- [x] I've tested on my platform: Linux (Hermes from source), end-to-end with a real model (gpt-oss-20b) driving the agent

### Documentation & Housekeeping

- [x] README with a disclosure section and SECURITY.md (threat model, limits) in the plugin repo
- [x] Cross-platform: Linux tested; Windows/macOS paths and locks handled in code (msvcrt/fcntl), not yet run on those systems

## Screenshots / Logs

![Ark page in Hermes Desktop](https://raw.githubusercontent.com/medomohamed22/hermes-ark/a23e529c1f636aa49d974576cdc5cb15fc8242d3/docs/media/desktop-dark.png)
![The agent relays Ark's health guard](https://raw.githubusercontent.com/medomohamed22/hermes-ark/a23e529c1f636aa49d974576cdc5cb15fc8242d3/docs/media/chat-guard.png)
![Restore preview](https://raw.githubusercontent.com/medomohamed22/hermes-ark/a23e529c1f636aa49d974576cdc5cb15fc8242d3/docs/media/restore-preview.png)
