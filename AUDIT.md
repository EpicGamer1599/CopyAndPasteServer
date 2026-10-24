# Code audit

Reviewed all project source, configuration, dependency manifests, tests, documentation and persistence directories. Generated caches and the virtual environment were excluded from source review; the installed discord.py 2.7.1 HTTP and serialization boundaries were inspected where relevant.

## Architecture and baseline

- main.py registers five private slash commands and handles independent administrator checks in source and destination, previews, confirmations, and background tasks.
- structure.py captures templates, maps complete role overwrites, plans blocked entries, creates channels with their parents, checkpoints IDs and verifies ordering.
- storage.py provides atomic JSON replacement and a process lock. snapshots/ and jobs/ contained only their placeholder files before the audit.
- Baseline: 36 tests passed on Python 3.13 / discord.py 2.7.1.

## Findings fixed

| Severity | Problem | Resolution |
| --- | --- | --- |
| High | discord.py internally retries POSTs after some 5xx responses and connection resets, allowing duplicate channels before the application can observe an uncertain result. | Per-create HTTP trace guard blocks resends after uncertain outcomes while allowing received 429 rejections to retry. The pending journal remains set. |
| High | Bot checks fetched fresh permissions but capture and planning still used stale cached guild/member/role objects, potentially including channels no longer visible or incorrectly accepting/rejecting a copy. | Use a REST guild with its fetched roles and member throughout capture/copy; refresh role definitions before executing the copy plan. |
| Medium | Member.roles omits role IDs missing from the guild cache, so a freshly assigned administrator role could be ignored even after fetching its definition. | Combine raw fetched member role IDs with fresh role permissions. |
| Medium | Resume only checked tracked IDs existed. A moved child or retyped category could be edited/reused before a parent mismatch was reported. | Reject changed types/parents and colliding journal IDs before Discord mutations. |
| Medium | Syntactically valid but malformed JSON could crash listings or feed duplicate IDs, misleading overwrite metadata or wrong identities into execution. | Validate snapshot structure and settings, selected-file identity, journal identity and mappings. Skip malformed listing entries. Non-object JSON is rejected. |
| Medium | JSON rename durability on POSIX was not completed with a directory fsync, leaving checkpoint replacement vulnerable to power loss. | Fsync the containing directory after replacement on POSIX. |
| Low | A queued Cancel after Confirm could display “No changes made” while the confirmed operation was running. | Both controls respect the single-use flag. |
| Low | Invalid test guild IDs and noncanonical/out-of-range role IDs failed late or confusingly. | Validate configuration and numeric IDs before use; allow whitespace around mapping separators. |
| Low | Cancellation during channel creation retained pending but left no explanatory report. | Persist an interruption report while preserving the uncertain marker. |

## Verification

The regression suite exercises the real discord.py HTTP request implementation with aiohttp and a local HTTP server, plus real Discord channel serialization at a simulated network boundary. It checks server errors, rate limits, dropped connections, exact relative channel/category order, tied positions, uncategorized channels, parent relationships, authorization, persistence and recovery.

Dependency consistency: pip check passed. Source parsing and command registration passed. See the final task response for the test count.

## Limits and maintenance

No live Discord mutation was performed. Real server permissions, eligibility, limits and Discord position normalization still need a live test with two configured servers. External edits during a copy remain outside the transaction boundary; verification reports mismatches. An uncertain create still requires manual journal reconciliation because Discord provides no create idempotency key.

The adapter depends on discord.py 2.7 internals for raw member role IDs, fresh guild cache binding, and bulk positions. Keep the 2.7 dependency constraint and regression tests when upgrading. HTTP trace support is documented in the [discord.py API](https://discordpy.readthedocs.io/en/stable/api.html#discord.Client); channel creation and overwrite rules are described in the [official Discord guild resource](https://github.com/discord/discord-api-docs/blob/main/developers/resources/guild.mdx).

## Follow-up: automatic departure after snapshots

The updated snapshot command was reviewed with a 65-test passing baseline. Fixed overlapping snapshot/departure and destination-copy operations by sharing the per-server operation lock. Notification transport failures now log the saved snapshot ID without reporting that the successful scan failed, and a failed departure warning cannot mask persistence success. Preview attachments and their caller-owned in-memory buffers are closed even when delivery fails.

Added six regression tests for concurrent commands, simultaneous notification/departure failures, network errors, lock release after a failed scan and attachment cleanup. Full suite: 71 tests passed. No live Discord operations were performed.
