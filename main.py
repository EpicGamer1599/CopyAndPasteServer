"""Discord server structure templates. Python 3.12+ / discord.py 2.7."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

from safe_http import create_trace
from storage import Store, acquire_process_lock
from structure import Copier, api_error, capture, counts, parse_role_map, plan, preview, validate_snapshot

ROOT = Path(__file__).resolve().parent
store = Store(ROOT)
copier = Copier(store)


def fingerprint(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


def effective_guild_permissions(guild_id, owner_id, member, roles):
    """Use freshly fetched role definitions, not cached permission flags."""
    if member.id == owner_id:
        return discord.Permissions.all()
    # Member.roles filters through the guild cache and can omit freshly added roles.
    ids = set(member._roles) | {guild_id}
    value = 0
    for role in roles:
        if role.id in ids:
            value |= role.permissions.value
    permissions = discord.Permissions(value)
    return discord.Permissions.all() if permissions.administrator else permissions


class StructureBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none(),
                         http_trace=create_trace())
        self.tree = app_commands.CommandTree(self)
        self.tasks = set()

    async def setup_hook(self):
        test_id = os.getenv("TEST_GUILD_ID")
        if test_id:
            target = discord.Object(id=int(test_id))
            self.tree.copy_global_to(guild=target)
            await self.tree.sync(guild=target)
        else:
            await self.tree.sync()

    async def on_ready(self):
        logging.getLogger(__name__).info("Bot ready; connected to %d guild(s).", len(self.guilds))

    async def close(self):
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        await super().close()


bot = StructureBot()


async def require_admin(user_id: int, guild_id: int):
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise ValueError("Bot is not connected to that server.")
    try:
        member = await guild.fetch_member(user_id)
        fresh = await bot.fetch_guild(guild_id)
        roles = await guild.fetch_roles()
    except (discord.NotFound, discord.Forbidden):
        raise ValueError("Unable to verify your administrator membership in that server.") from None
    if not effective_guild_permissions(guild.id, fresh.owner_id, member, roles).administrator:
        raise ValueError("You must currently be an Administrator in that server.")
    return guild


async def require_bot(guild, needs_overwrites=False, snapshot_only=False):
    # A REST guild owns fresh role definitions; bind its fetched bot member to it.
    guild = await bot.fetch_guild(guild.id)
    roles = await guild.fetch_roles()
    guild._roles = {role.id: role for role in roles}
    member = await guild.fetch_member(bot.user.id)
    guild._add_member(member)
    fresh = guild
    permissions = effective_guild_permissions(guild.id, fresh.owner_id, member, roles)
    if not permissions.view_channel or (not snapshot_only and not permissions.manage_channels):
        raise ValueError("Bot requires View Channels and, for copies, Manage Channels.")
    if needs_overwrites and not permissions.manage_roles:
        raise ValueError("Bot requires Manage Roles to recreate permission overwrites.")
    return guild


async def source_snapshot(interaction, snapshot_id):
    if interaction.guild_id is None:
        raise ValueError("Run this command in the snapshot's source server.")
    guild = await require_admin(interaction.user.id, interaction.guild_id)
    snapshot = store.read("snapshots", snapshot_id)
    validate_snapshot(snapshot)
    if snapshot["id"] != snapshot_id or snapshot["source"]["id"] != str(guild.id):
        raise ValueError("Select a snapshot belonging to this source server.")
    return snapshot, guild


async def reply(interaction, content, **kwargs):
    kwargs["ephemeral"] = True
    kwargs["allowed_mentions"] = discord.AllowedMentions.none()
    if interaction.response.is_done():
        return await interaction.followup.send(content, **kwargs)
    return await interaction.response.send_message(content, **kwargs)


def text_file(content, name):
    return discord.File(io.BytesIO(content.encode("utf-8")), filename=name)


class Confirmation(discord.ui.View):
    def __init__(self, user_id, action):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.action = action
        self.used = False

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await reply(interaction, "Only the requesting administrator can confirm.")
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if self.used:
            await reply(interaction, "This confirmation was already used.")
            return
        self.used = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Confirmation received. Checking authorization again…", view=self)
        self.stop()
        try:
            await self.action(interaction)
        except Exception as error:
            await report_error(interaction, error)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        if self.used:
            await reply(interaction, "This confirmation was already used.")
            return
        self.used = True
        self.stop()
        await interaction.response.edit_message(content="Cancelled. No changes made.", view=None)


def saved_snapshots():
    for item in store.list_snapshots():
        try:
            validate_snapshot(item)
        except ValueError:
            logging.getLogger(__name__).warning("Skipping malformed snapshot metadata.")
            continue
        yield item


async def snapshot_choices(interaction, current):
    # Autocomplete never exposes another server's metadata, even before checks.
    if interaction.guild_id is None or not interaction.permissions.administrator:
        return []
    choices = []
    for item in saved_snapshots():
        if item.get("source", {}).get("id") != str(interaction.guild_id):
            continue
        label = f"{item['created_at'][:19]} · {item['source']['name']} · {len(item['channels'])} entries · {item['id'][:8]}"
        if current.casefold() in (label + item["id"]).casefold():
            choices.append(app_commands.Choice(name=label[:100], value=item["id"]))
    return choices[:25]


async def destination_choices(interaction, current):
    # A cached membership is a discovery hint only. Execution always uses REST.
    choices = []
    for guild in bot.guilds:
        if guild.id == interaction.guild_id:
            continue
        member = guild.get_member(interaction.user.id)
        if member is None or not member.guild_permissions.administrator:
            continue
        label = f"{guild.name} ({guild.id})"
        if current.casefold() in label.casefold():
            choices.append(app_commands.Choice(name=label[:100], value=str(guild.id)))
    return choices[:25]


@bot.tree.command(name="snapshot", description="Save this server's visible structure, then leave the server.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def snapshot_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = await require_admin(interaction.user.id, interaction.guild_id)
    # Snapshot departure and destination copies must never overlap in one guild.
    lock = copier.locks.setdefault(guild.id, asyncio.Lock())
    if lock.locked():
        raise ValueError("Another snapshot or copy in this server is running; retry later.")
    async with lock:
        guild = await require_bot(guild, snapshot_only=True)
        snapshot = capture(guild, await guild.fetch_channels())
        store.write("snapshots", snapshot["id"], snapshot)
        # Only leave after capture and durable storage succeed. Notification
        # transport errors do not undo that success or prevent departure.
        try:
            attachment = text_file(preview(snapshot), "snapshot-preview.txt")
            try:
                await reply(interaction, f"Saved snapshot `{snapshot['id']}`: {counts(snapshot)}. "
                            "Scanning is complete; I am leaving this source server now. "
                            "Re-invite me before copying or managing this snapshot so I can verify source administrators. "
                            "Capture limitations are in the attached preview.", file=attachment)
            except (discord.HTTPException, discord.RateLimited, aiohttp.ClientError, OSError) as error:
                logging.getLogger(__name__).warning("Snapshot %s saved; notification failed: %s",
                                                    snapshot["id"], type(error).__name__)
            finally:
                attachment.close()
        finally:
            try:
                await guild.leave()
            except (discord.HTTPException, discord.RateLimited, aiohttp.ClientError, OSError) as error:
                logging.getLogger(__name__).warning("Snapshot %s saved; source departure unconfirmed: %s",
                                                    snapshot["id"], type(error).__name__)
                try:
                    await reply(interaction, f"Snapshot `{snapshot['id']}` is safely saved, but I could not confirm leaving "
                                f"the source server ({api_error(error)}). If I am still present, remove me manually.")
                except (discord.HTTPException, discord.RateLimited, aiohttp.ClientError, OSError) as notification_error:
                    logging.getLogger(__name__).warning("Snapshot %s saved; departure warning could not be delivered: %s",
                                                        snapshot["id"], type(notification_error).__name__)


@bot.tree.command(name="preview", description="Show a saved structure in its original order.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.autocomplete(snapshot_id=snapshot_choices)
async def preview_command(interaction: discord.Interaction, snapshot_id: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    snapshot, _ = await source_snapshot(interaction, snapshot_id)
    await reply(interaction, f"{counts(snapshot)}. Full ordered structure attached.",
                file=text_file(preview(snapshot), "structure.txt"))


@bot.tree.command(name="snapshots", description="List snapshots belonging to this source server.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def snapshots_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    await require_admin(interaction.user.id, interaction.guild_id)
    rows = [f"{s['id']} | {s['source']['name']} | {s['created_at']} | {counts(s)}"
            for s in saved_snapshots() if s["source"]["id"] == str(interaction.guild_id)]
    await reply(interaction, f"{len(rows)} saved snapshots for this server.",
                file=text_file("\n".join(rows) or "No snapshots yet. Use /snapshot.", "snapshots.txt"))


@bot.tree.command(name="delete_snapshot", description="Delete a local snapshot after confirmation; never deletes Discord channels.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.autocomplete(snapshot_id=snapshot_choices)
async def delete_snapshot_command(interaction: discord.Interaction, snapshot_id: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    snapshot, _ = await source_snapshot(interaction, snapshot_id)
    digest = fingerprint(snapshot)
    async def action(click):
        current, _ = await source_snapshot(click, snapshot_id)
        if fingerprint(current) != digest:
            raise ValueError("Snapshot changed; request a new confirmation.")
        store.delete(snapshot_id)
        await reply(click, "Snapshot deleted. Existing Discord channels and copy journals were retained.")
    await reply(interaction, f"Delete snapshot `{snapshot_id}` ({counts(snapshot)})? Confirmation expires in two minutes.",
                view=Confirmation(interaction.user.id, action))


async def copy_access(interaction, snapshot_id, destination_id):
    snapshot, source = await source_snapshot(interaction, snapshot_id)
    if (not destination_id.isascii() or not destination_id.isdecimal()
            or not 0 < int(destination_id) < 2**64):
        raise ValueError("Destination must be a numeric server ID.")
    destination = await require_admin(interaction.user.id, int(destination_id))
    if destination.id == source.id:
        raise ValueError("Select a separate destination server.")
    destination = await require_bot(destination, any(c["overwrites"] for c in snapshot["channels"]))
    return snapshot, destination


@bot.tree.command(name="copy_structure", description="Preview and confirm an append-only copy, or resume a previous copy.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(destination_id="Destination server ID (you must administer it).",
                       role_map="Optional explicit source:destination role IDs, separated by commas.")
@app_commands.autocomplete(snapshot_id=snapshot_choices, destination_id=destination_choices)
async def copy_structure_command(interaction: discord.Interaction, snapshot_id: str,
                                 destination_id: str, role_map: str | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    snapshot, destination = await copy_access(interaction, snapshot_id, destination_id)
    mapping = parse_role_map(role_map)
    roles = await destination.fetch_roles()
    job = copier.job(snapshot, destination)
    blocked = plan(snapshot, destination, roles, mapping, job)
    new = [c for c in snapshot["channels"] if c["id"] not in job["created"] and c["id"] not in blocked]
    planned = {**snapshot, "channels": new}
    digest = fingerprint(snapshot)
    job_digest = fingerprint(job)
    async def action(click):
        current, target = await copy_access(click, snapshot_id, destination_id)
        if fingerprint(current) != digest or fingerprint(copier.job(current, target)) != job_digest:
            raise ValueError("Snapshot or copy state changed; request a fresh preview and confirmation.")
        # Own the task independently of the interaction's 15-minute token lifetime.
        task = asyncio.create_task(run_copy(click, current, target, mapping))
        bot.tasks.add(task)
        task.add_done_callback(bot.tasks.discard)
        await reply(click, "Copy started. The durable report is saved in jobs/. Large copies may outlive Discord's response window; rerun this command to inspect/resume.")
    lines = [f"Destination: {destination.name} ({destination.id})",
             f"Will create: {counts(planned)} ({len(new)} entries).",
             f"Already tracked: {len(job['created'])}. Blocked: {len(blocked)}.",
             "Existing channels are kept. Copied entries are appended within Discord's ordering groups.",
             "Explicit role mappings assert equivalent access intent; review destination role membership yourself.",
             "@everyone is mapped automatically. No other roles are matched by name or ID.",
             "", *[f"BLOCKED {key}: {value}" for key, value in blocked.items()],
             "", preview(snapshot), "", "Previous copy report:", *job.get("report", [])]
    await reply(interaction, f"Ready to create {len(new)} entries in **{discord.utils.escape_markdown(destination.name)}**; "
                f"{len(blocked)} blocked, {len(job['created'])} tracked. Confirm within two minutes.",
                file=text_file("\n".join(lines), "copy-plan.txt"),
                view=Confirmation(interaction.user.id, action))


async def run_copy(interaction, snapshot, destination, mapping):
    try:
        report = await copier.copy(snapshot, destination, mapping,
                                   f"Structure template {snapshot['id']}")
        try:
            await reply(interaction, "Copy finished. Review the attached report for skipped entries, partial failures and limitations.",
                        file=text_file(report, "copy-report.txt"))
        except discord.HTTPException:
            logging.getLogger(__name__).info("Interaction expired; copy report retained locally in jobs/.")
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logging.getLogger(__name__).error("Copy stopped: %s", type(error).__name__)
        try:
            await report_error(interaction, error)
        except discord.HTTPException:
            pass


async def report_error(interaction, error):
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original
    if isinstance(error, ValueError):
        message = str(error)
    elif isinstance(error, FileNotFoundError):
        message = "Snapshot not found. Use /snapshots to select a saved ID."
    elif isinstance(error, app_commands.AppCommandError):
        message = "This command requires a server Administrator."
    elif isinstance(error, discord.InvalidData):
        message = "Discord returned a channel type/data this library cannot parse. No snapshot was saved; update discord.py or inspect the server's unsupported channel types."
    elif isinstance(error, (discord.HTTPException, discord.RateLimited)):
        message = api_error(error) + ". Check permissions and retry. Copy progress is kept in jobs/."
    else:
        message = f"Operation stopped ({type(error).__name__}). Inspect local data and retry; saved progress is retained."
    logging.getLogger(__name__).warning("Command stopped: %s", type(error).__name__)
    await reply(interaction, message[:1900])


bot.tree.error(report_error)


def main():
    load_dotenv(ROOT / ".env")
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "replace_with_your_bot_token":
        raise SystemExit("Set DISCORD_TOKEN in .env before starting the bot.")
    test_id = os.getenv("TEST_GUILD_ID")
    if test_id and (not test_id.isascii() or not test_id.isdecimal() or not 0 < int(test_id) < 2**64):
        raise SystemExit("TEST_GUILD_ID must be a positive Discord server ID.")
    lock = acquire_process_lock(ROOT)
    try:
        # Suppress library HTTP logging: request diagnostics must never contain credentials.
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        logging.getLogger("discord.http").setLevel(logging.CRITICAL)
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        raise SystemExit("Discord rejected the bot token. Update DISCORD_TOKEN in .env.") from None
    finally:
        lock.close()


if __name__ == "__main__":
    main()
