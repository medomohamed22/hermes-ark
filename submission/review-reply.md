Thanks for the review, @teknium1. All four items are fixed and `sha:` is re-pinned to `a23e529c1f636aa49d974576cdc5cb15fc8242d3` (version 0.6.3). Delta: https://github.com/medomohamed22/hermes-ark/compare/f2d5378724134dd62010df4def48ad2e0dad761e...a23e529c1f636aa49d974576cdc5cb15fc8242d3

1. **No write-back.** `repair()` no longer restores settings or enabled state; `_enable_via_cli`, `_save_enabled` and the subprocess are gone. The pre-call block and the tamper alarm stay, and the user's change sticks. The only repair left is Ark's own repository key file (`ark.json`) when an agent tool call deleted it.
2. **The watchdog stops** when the plugin is unloaded (`ctx.on_unload`) or disabled (checked every tick). I verified it in a live gateway: the `hermes-ark-watchdog` thread is gone after `hermes plugins disable hermes-ark`, and the config stays disabled.
3. **Code self-restore removed.** Ark keeps no copies of its files and never rewrites or deletes any. A change to its code only raises the alarm.
4. **`include_secrets` removed.** `.env`, `auth.json`, the vault and pairing files are never captured, restored or deleted. Restore also skips them in snapshots made by older versions.

Tests assert the new behaviour (174 passed). One test checks that none of the write-back code remains. `hermes plugins validate --install-deps` passes at the new pin (security scan: safe). README, SECURITY.md and the catalog disclosure are updated, and I updated the PR description.
