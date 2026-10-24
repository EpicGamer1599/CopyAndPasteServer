# CAPS: advanced reference

New to CAPS? Start with [the quick guide](../README.md) or `/help` in Discord. Run the shell commands below from the project root, not from `docs/`.

A working Python 3.12+ bot using discord.py 2.7. It saves visible channel/category templates as local JSON and copies them into a different server, alongside existing channels. No destination deletion or replacement mode is implemented.

## Setup

1. Install Python 3.12 or newer. In the [Discord Developer Portal](https://discord.com/developers/applications), create an application, open **Bot**, and create/reset its bot token. Use a standard bot account. Keep the token private.
2. In **Installation** / **OAuth2 URL Generator**, choose **Guild Install**, the `bot` and `applications.commands` scopes, and bot permissions **View Channels**, **Manage Channels**, **Manage Roles** (needed for overwrites), **Send Messages**, and **Attach Files**. Invite the same bot into both source and destination servers. You need permission to install it in each.
3. In the source, give the bot View Channels on every channel/category to capture. In the destination, ensure channel overwrites do not deny the bot View Channels or Manage Channels. Keep its role above roles it needs to manage. The API can only set overwrite permission bits the bot is permitted to set; Administrator is needed for some bits such as Manage Roles. Grant Administrator only if you deliberately want complete access for copying restrictive layouts. Otherwise limitations will be reported as permission errors. Privileged **Members**, **Message Content**, and **Presence** gateway intents are unnecessary; leave them disabled.
4. Create the environment and configuration from the project directory:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   Copy-Item .env.example .env
   ```

   Edit `.env` locally and replace the placeholder in `DISCORD_TOKEN`. Do not paste the token into Discord or source code. An existing environment variable takes precedence over `.env`.

   On macOS/Linux use `python3 -m venv .venv`, `.venv/bin/python -m pip install -r requirements.txt`, and `cp .env.example .env`.

5. Start the bot:

   ```powershell
   .\.venv\Scripts\python.exe main.py
   ```

   On macOS/Linux: `.venv/bin/python main.py`. Keep the process running. Stop with Ctrl+C. Data paths are relative to `main.py`, regardless of your shell's working directory.

Commands sync globally at startup and can take time to appear. For development, `TEST_GUILD_ID` restricts command syncing to one server; remove it for normal use because the beginner workflow runs `/copy` in the receiving server. Old guild-scoped registrations can remain after switching modes; remove them through Discord's application integration settings if needed.

## Commands

All commands run in a server and respond privately. Template commands require current Administrator membership; `/help` is available to everyone. Template commands work from any server you administer. They separately verify your current administrator membership in the template's original server before showing its contents or using it. The bot fetches membership and role permissions from Discord again when a copy/deletion is confirmed. Bot visibility and permission checks use a fresh REST guild/member/role context; knowing a guild ID is never sufficient authorization.

| Command | Behavior |
| --- | --- |
| `/scan name` | Fetch channels, save visible structure in `snapshots/<id>.json`, attach a preview, then automatically leave the source server. |
| `/templates` | Show accessible saved layouts by name, date and count, with a paged preview menu. |
| `/preview template` | Attach the full ordered tree, IDs, category/channel totals, and limitations. |
| `/copy template server roles` | Show types/counts to create, previously tracked entries, blocked entries, full structure, and any previous report. Press Create channels within two minutes to confirm. All arguments are optional. |
| `/delete template` | Require Confirm before deleting the local snapshot; Discord channels and copy journals are retained. |

Run `/copy` **in the receiving server**. The server option defaults to that server. Choose a template by name, or leave it blank to use the latest accessible layout from another server. Missing roles are created automatically when you confirm; no matching wizard is required. Use existing roles (optional) offers a role picker if you prefer to reuse roles explicitly. The optional server override still accepts an ID. Template selection supports autocomplete and `/templates` provides a paged preview menu. Destination suggestions appear only for servers where the bot already has a cached administrator membership for you; without the Members intent, this list may be empty. You can always enter the destination ID manually; it is checked against your current membership through the API.

After `/scan` saves successfully, the bot leaves **only the source server** using Discord's self-leave endpoint; it needs no Kick Members permission. It sends the saved ID and preview before leaving, and retains the snapshot on disk. Capture or save failures keep it in the source so you can retry. A notification failure after saving does not prevent departure or turn a saved scan into a failed scan. If Discord rejects the leave request or its result is uncertain, the bot attempts to report that the snapshot is saved and asks you to remove it manually if it remains present. If Discord cannot deliver either notification, the application log records the saved snapshot ID and the failed operation; the snapshot remains in `snapshots/`.

Snapshots and copies into the same server share an operation lock. An overlapping command is rejected before scanning or creating channels, so automatic departure cannot interrupt an active destination copy. Retry after the running operation finishes; re-invite the bot if the completed operation was a snapshot.

**Re-invite the bot to the original server before previewing, listing, deleting or copying that template.** You can then run these commands in the receiving server. The existing source-administrator checks require membership in that server; a saved snapshot ID alone does not grant access. Once re-invited, use `/copy` directly with the saved ID instead of taking another `/scan` (which would make the bot leave again). The bot stays in the destination after copying.

Only the requester can use a confirmation. Cancel or an expired confirmation creates/deletes nothing. A copy that you confirmed continues in the background even if Discord's interaction token expires. Its durable report remains in `jobs/`; rerunning the command shows the previous report before asking for another confirmation.

### Permission mapping

`@everyone` maps to the destination's own default role. For every source role without a saved or explicit mapping, CAPS creates a new unmanaged destination role with the source role's current name, no members, no server-wide permission bits, and mentionable/hoist disabled. Its actual returned ID is checkpointed before any channel creation. Existing roles are never automatically matched by name or coincidentally equal IDs. Only channel overwrites are transferred; original guild role powers, hierarchy and memberships are not cloned. If the original role name is unavailable, an Imported role ID label is used, so old snapshots remain usable without rescanning.

To reuse existing roles, choose Use existing roles (optional) in the receiving server, or provide the advanced roles option, such as:

```text
/copy template:<saved-id> server:222222222222222222 roles:111111111111111111:333333333333333333,444444444444444444:555555555555555555
```

For the optional advanced roles field, each pair is `original_role_id:receiving_role_id`. Choosing a role or supplying a mapping is your assertion that the roles serve equivalent access purposes. The bot never reads/copies member lists. Mappings to managed/integration roles or to `@everyone` for a non-default role are rejected. Unmapped roles are created automatically rather than blocking categories and their children. No partial overwrite set is applied. Saved mappings are reused on resume, including for copies started before automatic role creation was added.

Member-specific overwrites are recorded only as an omission flag, without member IDs, names, or permission data. Those channels are blocked from copying, including their children when the affected entry is a category. To use such a template, redesign access with roles in the source and take a new snapshot. The bot does not silently drop restrictive overwrites and create a more public channel.

### Ordering and settings

The snapshot uses Discord's official channel-list order: uncategorized channels first, categories ordered by position/ID, and children ordered within Discord's text-like and voice-like sorting groups by position/ID. This matches `Guild.by_category()` while using fresh REST channel objects. Empty categories and parent references are retained. Discord has one category level; nested **channels** are supported, but nested **categories** do not exist in Discord.

Text, voice, announcement, stage, forum, and media channels are created using discord.py's corresponding guild creation methods. Settings include names, text/forum topics, slowmode, NSFW, archive defaults, thread slowmode defaults, voice bitrate/limit/region/video quality, forum tags, tag requirement, Unicode default reactions, forum sort order and layout, and role overwrites. Voice slowmode, category NSFW and forum tag requirement are applied as checkpointed, retryable follow-up edits.

Channels are created with their destination parents and complete mapped overwrites. Positions are reapplied in one bulk request afterward, and a fresh fetch verifies copied-entry order and parent relationships. Existing channels retain their membership and relative order; their numeric positions may be normalized. New entries follow existing entries within each Discord sorting group. Uncategorized channels still appear above categories because Discord enforces that grouping. Avoid concurrent manual edits while copying. On resume, settings edits and positions may be reapplied to already-created channels.

discord.py 2.7 has no public bulk channel position method. `reapply_positions()` is the single compatibility boundary using its internal `HTTPClient.bulk_channel_update` against the official endpoint, with the library's standard rate-limit handling. Requirements constrain discord.py to the tested 2.7 series; check this boundary when upgrading.

## Recovery and persistent files

- `snapshots/`: UTF-8 JSON templates, with schema version, source guild ID/name, UTC timestamp, original channel IDs, parent IDs, positions, settings, transferable role overwrites and omission reports.
- `jobs/<snapshot-id>_<destination-id>.json`: durable source-to-destination channel ID map, snapshot digest, explicit role map, pending create marker, and latest report. Keep this file to prevent duplication. Keep a backup of both directories; they are ignored by Git.
- `.bot.lock`: OS process lock. One bot instance may use a data directory. The OS releases the lock after a crash; the file itself can remain.

Rerun `/copy` with the **same snapshot and destination**, to resume; saved role mappings are reused automatically. It shows a fresh plan and requires confirmation again. Completed channel IDs are skipped. Additional role mappings may be supplied for previously blocked entries, but existing mappings cannot change. Missing destination channels, changed channel types/parents, malformed journals or a modified snapshot stop a retry rather than trigger replacement. Restore the intended type/parent before retrying; the bot does not silently reparent channels that were edited externally. Take a new snapshot for a deliberately separate copy.

discord.py handles ordinary API rate limits. Definite rejected requests (4xx) are reported and can be retried after fixing permissions, server limits or feature requirements. A surfaced rate-limit exception stops the run safely. Position/settings failures can be retried without recreating channels. API failures stop creation for that run; later entries remain uncreated. Role creation additionally requires Manage Roles and is subject to Discord's role limits.

Discord's channel-create endpoint has no caller-provided idempotency key. A timeout, network failure, 5xx response, unexpected response, process crash, or disk failure after a request can leave its result uncertain. The journal writes `pending` **before** creating each channel and checkpoints the returned ID immediately afterward. An uncertain operation blocks further copying instead of issuing another create request. `safe_http.py` uses the documented `Client.http_trace` hook to prevent discord.py from resending a channel POST after an ambiguous result. Received 429 rejections may still retry normally. This hook must remain configured on the bot when changing its HTTP setup.

To reconcile an uncertain operation:

If `pending_role` is set instead of `pending`, reconcile the role create first: while the bot is stopped, verify the outcome using the destination's roles and audit log. If created, add its source-to-destination ID pair (both strings) to `role_map`, then set `pending_role` to `null`. If confirmed not created, only clear `pending_role`. Leave it pending if uncertain. Do not clear other mappings or channel progress. Role POSTs use the same HTTP retry guard as channel POSTs.

1. Stop the bot and back up the journal. Read its `pending` source channel ID and the corresponding source snapshot entry.
2. Inspect the destination channel list and audit log manually to establish whether that exact create succeeded. Compare type, name, parent and overwrites; names alone are not unique. Older bot versions without the HTTP retry guard and external edits can require additional inspection.
3. If exactly one corresponding destination channel exists, add `"<source-channel-id>": <destination-channel-id as a JSON integer>` to the journal's `created` object and set `pending` to `null`. Check settings and overwrites manually before marking it adopted. If you positively established that no channel was created, just set `pending` to `null`. If the outcome is still ambiguous, leave it pending.
4. Preserve the snapshot digest, prior mappings and all other created IDs. Restart and run the confirmed copy again. Do not delete the journal or reset all progress to recover one channel.

For a previously tracked channel that was manually deleted, restore it externally or, while the bot is stopped, remove only that verified-missing entry from `created` to explicitly allow its recreation on the next confirmed copy. The bot never does this automatically. It also never automatically deletes duplicate, existing, or partially created destination channels.

Protect the project directory with normal OS access controls. Snapshots intentionally contain channel names/topics and role access rules; review template text for sensitive information. No messages, member lists, member identifiers from overwrites, webhooks, tokens, or credentials are saved in these files. The `.env` and persistent JSON files are excluded from Git. HTTP error bodies and credentials are not emitted in command responses or application error logs.

## Discord limitations

- Only bot-visible structure can be captured. Client-specific collapsed categories, favorites, onboarding/personalized views and hidden-channel layouts are not available as a shared server template.
- Announcement, stage, forum and media creation depends on Community and any additional destination feature eligibility. Media support is still evolving; a server-side rejection is reported with its HTTP status/code. Destination channel/category limits and voice bitrate limits apply. Bitrates above the destination limit are blocked rather than silently reduced.
- Live stage topics/sessions, forum posts/threads, messages, integrations, followers, webhooks, role definitions, member data, guild configuration and custom emoji assets are excluded. Forum tags receive new IDs. Unicode tag/reaction emoji are preserved; custom emoji are omitted and reported. Media uses its fixed layout. Additional channel flags unsupported by this bot are recorded as omissions.
- The API supplies position/ID ordering, not a per-user visual view. Existing destination entries mean absolute numeric positions differ; relative order among copied entries is preserved. A verification mismatch is reported and positions can be retried. Concurrent external changes are outside the bot's transaction boundary.
- If Discord introduces a completely unknown channel type that discord.py cannot deserialize, snapshot fetching stops with a clear error rather than producing a silently incomplete template. Known unsupported types are blocked and reported. Update the library/adapter when adding support.
- Discord operations are not transactional. Successful channels remain after a later failure; every copy reports its tracked count and limitations. Long runs may exceed the interaction response window, so inspect the local report or rerun for the previous report.

References: [discord.py API](https://discordpy.readthedocs.io/en/stable/api.html), [Discord guild/channel creation and positions](https://github.com/discord/discord-api-docs/blob/main/developers/resources/guild.mdx), and [Discord forum/media behavior](https://github.com/discord/discord-api-docs/blob/main/developers/topics/threads.mdx).

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests need no token or Discord server. HTTP retry tests start a temporary local loopback server. They use real discord.py guild/channel/role objects and creation/edit serialization with a simulated HTTP boundary. Coverage includes UI ordering (and comparison to the library), tied positions, category children, empty categories, uncategorized channels, role mapping and member omission, permission checks, confirmations, storage, all supported creation types, partial failures, timeouts, rate limits, position/settings retries, and duplicate prevention. A live end-to-end test requires your configured token and two servers; none is bundled.

The audit regression suite also covers ambiguous HTTP retries, independent source/destination checks, fresh permission contexts, moved/retyped tracked channels, malformed snapshots/journals, interrupted creates, checkpoint replacement failures, and confirmation races. See [AUDIT.md](../AUDIT.md) for findings and verification limits.
