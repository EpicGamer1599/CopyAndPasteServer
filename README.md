# CopyAndPasteServer - CAPS :3

Save a Discord server's channel layout, then create that layout in another server. CAPS copies categories, channels, settings and supported permissions. It keeps the receiving server's existing channels.

**Messages, members and conversations are never copied.** You must be an administrator in both servers.

## Using the bot

Already have CAPS running? Type **`/help`** in Discord for the same guide inside the bot.

1. **Save the original server.** Invite CAPS there and run `/scan`. You can add a name, such as `/scan name:Gaming layout`. A **template** just means a saved channel layout.
2. **CAPS saves the layout and leaves automatically.** Its reply shows what was saved and gives you a re-invite link.
3. **Invite CAPS back to the original server once.** It needs to check that you still administer that server. **Do not scan again**: that would make it leave again.
4. **Go to the receiving server.** Invite CAPS there, then run `/copy`. Choose your template from the suggestions. If you leave template blank, CAPS uses the newest accessible template from another server.
5. **Review the plan.** It names both servers and shows what will be created or skipped. If it offers **Match permissions**, use the role picker to choose equivalent roles in this server.
6. **Press Create channels.** Nothing is created before you confirm. Wait for the result and read any skipped-channel notes.

You usually do **not** need to type server IDs, template IDs or role IDs. `/copy` uses the server you are in. For multiple saved layouts, choose one by name and date.

## Commands

| Command | What it does |
| --- | --- |
| `/help` | Shows the steps above and explains what to do next. |
| `/scan` | Saves this server's layout, then leaves this server. Optional: give it a `name`. |
| `/templates` | Shows saved layouts you can access. Choose one from the menu to preview it. |
| `/preview` | Shows a saved layout's channels in order. Optional: choose a `template`; otherwise shows the newest one you can access. |
| `/copy` | Shows a plan for creating a saved layout in **this server**, then asks you to confirm. Optional: choose a `template`. |
| `/delete template` | Deletes a selected saved layout after confirmation. Does not delete any Discord channels. |

The old `/snapshot`, `/snapshots`, `/copy_structure` and `/delete_snapshot` names have been replaced by `/scan`, `/templates`, `/copy` and `/delete`. Restart the updated bot to sync the new names. Global command updates can take time to appear.

All template commands require Administrator. Replies are private. **Only `/scan` makes the bot leave.**

### Matching permissions

Different servers have different roles. CAPS cannot assume that two roles with the same name have the same members or purpose.

When the plan shows channels needing role matches:

1. Press **Match permissions**.
2. CAPS names an original role, such as **Moderators**.
3. Choose the role that should get the same channel access in the receiving server.
4. Repeat for the remaining roles, then review the updated plan and confirm.

Create missing roles under **Server Settings → Roles** first. Check who has each role before selecting it. CAPS never creates roles or copies role memberships. The `@everyone` role is matched automatically.

**Skip remaining** leaves channels needing those roles out of the copy. Channels with permissions assigned to individual people are also skipped, so private channels are not accidentally made public. The result explains what was skipped.

## Running your own bot

You need **Python 3.12 or newer**, a Discord bot token, and this project folder.

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an application and open **Bot** to get its bot token. Keep the token private.
2. Enable **Guild Install**. Create an invite with scopes `bot` and `applications.commands` and permissions **View Channels**, **Manage Channels**, **Manage Roles**, **Send Messages**, and **Attach Files**. Invite the bot to both servers. Leave privileged gateway intents disabled.
3. Open PowerShell in this project folder and run:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   Copy-Item .env.example .env
   ```

4. Open `.env` in a text editor. Replace `replace_with_your_bot_token` with your token. Save it. Do not put the token in source code, Discord chat or GitHub.
5. Start CAPS:

   ```powershell
   .\.venv\Scripts\python.exe main.py
   ```

6. Keep that terminal open. In Discord, run `/help`. Stop the bot with **Ctrl+C**. Restart it after downloading code changes.

On macOS/Linux, use `python3 -m venv .venv`, `.venv/bin/python -m pip install -r requirements.txt`, `cp .env.example .env`, and `.venv/bin/python main.py` instead.

The bot needs access to every channel you want to scan. Restricted layouts may need additional permissions or Administrator to reproduce all permission settings. Grant it only the access you intend. It needs no Kick Members permission to leave.

## If something goes wrong

| What you see | What to do |
| --- | --- |
| No saved layouts are available | Run `/scan` in the original server. If you already did, re-invite CAPS there once, then retry `/templates` in the receiving server. |
| CAPS keeps leaving | `/scan` always leaves after saving. Re-invite it, then use `/copy` in the other server instead of scanning again. |
| Administrator required | Ask the owner to grant you Administrator in both servers, or have an administrator perform the copy. |
| Channels need matching roles | Press Match permissions and choose the equivalent receiving-server roles. |
| Missing bot permissions | Check CAPS's role and private-channel permissions. Start with View Channels, Manage Channels and Manage Roles. |
| Some channels were skipped | Read the plan/report. Community-only channel types, member-specific permissions and missing role matches can cause skips. |
| A copy stopped partway through | Run `/copy` again with the same template. Saved role matches and completed channels are reused. Review the new plan before confirming. |
| The report says the result is uncertain | Ask the person running CAPS to follow the recovery instructions below. Do not delete progress files to force another copy. |
| Commands are missing | Keep the bot running, check the invite has slash-command scope, and allow time for Discord to refresh commands. Remove `TEST_GUILD_ID` from `.env` for normal use in both servers. |

Saved layouts are in `snapshots/`. Copy progress and detailed reports are in `jobs/`. Back up both folders. They and `.env` are excluded from Git.

For advanced options (`server` and manual `roles`), exact supported settings, Discord limitations, permissions and recovery, see [the advanced reference](docs/ADVANCED.md). For the code review, see [AUDIT.md](AUDIT.md).

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests need no bot token. They cover ordering, category relationships, permission matching, authorization, confirmation, HTTP failures, duplicate prevention and recovery. A live Discord check requires your own token and two servers.
