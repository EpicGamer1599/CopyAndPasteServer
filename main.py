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

from friendly import (blocker_text, error_text, report_summary, safe, template_embed,
                      template_label, template_name, totals)
from safe_http import create_trace
from storage import Store, acquire_process_lock
from structure import Copier, api_error, capture, counts, missing_role_ids, parse_role_map, plan, preview, validate_snapshot

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


def invite_link(guild_id=None):
    if bot.user is None:
        return "Re-invite me"
    options = {"permissions": discord.Permissions(268471312), "scopes": ("bot", "applications.commands")}
    if guild_id is not None:
        options.update(guild=discord.Object(id=guild_id), disable_guild_select=True)
    return f"[Re-invite me]({discord.utils.oauth_url(bot.user.id, **options)})"


async def require_admin(user_id: int, guild_id: int):
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise ValueError("CAPS is not connected to that server. Invite me there, then retry. Use /help for the steps.")
    try:
        member = await guild.fetch_member(user_id)
        fresh = await bot.fetch_guild(guild_id)
        roles = await guild.fetch_roles()
    except (discord.NotFound, discord.Forbidden):
        raise ValueError("Unable to verify your administrator membership in that server.") from None
    if not effective_guild_permissions(guild.id, fresh.owner_id, member, roles).administrator:
        raise ValueError("You need the Administrator permission in that server. Ask the server owner to grant it or run the command for you.")
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
        raise ValueError("Use this command inside a server, not in a direct message. Start with /help.")
    guild = await require_admin(interaction.user.id, interaction.guild_id)
    if snapshot_id is None:
        available = await accessible_templates(interaction)
        if not available:
            raise ValueError("No saved layouts are available. Use /scan in the original server first. "
                             "If you already scanned it, re-invite me there so I can check that you are still an administrator.")
        snapshot_id = available[0]["id"]
    snapshot = store.read("snapshots", snapshot_id)
    validate_snapshot(snapshot)
    if snapshot["id"] != snapshot_id:
        raise ValueError("Choose a template belonging to a saved file from /templates.")
    source_id = int(snapshot["source"]["id"])
    if source_id != guild.id:
        if bot.get_guild(source_id) is None:
            raise ValueError(f"{invite_link(source_id)} to the original server, then retry this command. "
                             "I need to check that you are still an administrator there. Do not scan again.")
        guild = await require_admin(interaction.user.id, source_id)
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
    def __init__(self, user_id, action, label="Confirm"):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.action = action
        self.used = False
        self.confirm.label = label

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
        await interaction.response.edit_message(content="Checking your administrator permissions, then starting…", view=self)
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


async def accessible_templates(interaction):
    """Only disclose templates after current-server and source-server REST checks."""
    await require_admin(interaction.user.id, interaction.guild_id)
    snapshots = list(saved_snapshots())
    source_ids = {int(item["source"]["id"]) for item in snapshots}
    allowed = {interaction.guild_id}
    semaphore = asyncio.Semaphore(4)
    async def check(source_id):
        if source_id in allowed or bot.get_guild(source_id) is None:
            return
        async with semaphore:
            try:
                await require_admin(interaction.user.id, source_id)
            except (ValueError, discord.HTTPException, discord.RateLimited, aiohttp.ClientError, OSError):
                return
            allowed.add(source_id)
    await asyncio.gather(*(check(source_id) for source_id in source_ids))
    return sorted((item for item in snapshots if int(item["source"]["id"]) in allowed),
                  key=lambda item: item["created_at"], reverse=True)


async def snapshot_choices(interaction, current):
    if interaction.guild_id is None or not interaction.permissions.administrator:
        return []
    try:
        # Autocomplete has a short deadline; commands also work with no selection.
        available = await asyncio.wait_for(accessible_templates(interaction), timeout=2)
    except (TimeoutError, ValueError, discord.HTTPException, discord.RateLimited, aiohttp.ClientError, OSError):
        return []
    choices = []
    for item in available:
        label = template_label(item)
        if current.casefold() in (label + item["id"]).casefold():
            choices.append(app_commands.Choice(name=label[:100], value=item["id"]))
    return choices[:25]


async def destination_choices(interaction, current):
    # A cached membership is a discovery hint only. Execution always uses REST.
    choices = []
    for guild in bot.guilds:
        member = guild.get_member(interaction.user.id)
        if member is None or not member.guild_permissions.administrator:
            continue
        label = f"{guild.name} ({guild.id})"
        if current.casefold() in label.casefold():
            choices.append(app_commands.Choice(name=label[:100], value=str(guild.id)))
    return choices[:25]


@bot.tree.command(name="help", description="Start here: learn how to save and copy a server layout.")
@app_commands.guild_only()
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="CAPS — copy a server layout", colour=discord.Colour.blurple(),
        description="A **template** is a saved list of categories, channels and their settings. You must be an administrator in both servers.")
    embed.add_field(name="1. Save the original server", value="Invite CAPS there and use `/scan`. You can give the layout a name. CAPS saves it, then leaves automatically.", inline=False)
    embed.add_field(name="2. Invite CAPS back once", value=f"{invite_link()} to the original server so I can check your administrator access. **Do not scan again** — that would make me leave again.", inline=False)
    embed.add_field(name="3. Copy into your other server", value="Invite CAPS to the receiving server. Run `/copy` **there** and press **Create channels**. Missing roles are created automatically so channel permissions come over too. You do not need to match roles.", inline=False)
    embed.add_field(name="Other commands", value="`/templates` — see saved layouts\n`/preview` — see the channels in a layout\n`/delete` — delete a saved layout after confirming", inline=False)
    embed.set_footer(text="Existing channels stay. Messages and members are never copied. Only /scan makes CAPS leave.")
    await reply(interaction, None, embed=embed)


@bot.tree.command(name="scan", description="Save this server's channel layout. CAPS leaves this server after saving.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(name="Optional name for this saved layout, such as Gaming server.")
async def snapshot_command(interaction: discord.Interaction, name: app_commands.Range[str, 1, 60] | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = await require_admin(interaction.user.id, interaction.guild_id)
    # Snapshot departure and destination copies must never overlap in one guild.
    lock = copier.locks.setdefault(guild.id, asyncio.Lock())
    if lock.locked():
        raise ValueError("Another snapshot or copy in this server is running; retry later.")
    async with lock:
        guild = await require_bot(guild, snapshot_only=True)
        snapshot = capture(guild, await guild.fetch_channels())
        if name is not None:
            if not name.strip():
                raise ValueError("Please give your template a name instead of just spaces.")
            snapshot["label"] = name.strip()
        store.write("snapshots", snapshot["id"], snapshot)
        # Only leave after capture and durable storage succeed. Notification
        # transport errors do not undo that success or prevent departure.
        try:
            attachment = text_file(preview(snapshot), "snapshot-preview.txt")
            try:
                await reply(interaction, f"**Layout saved: {safe(template_name(snapshot))}**\n"
                            f"Saved {totals(snapshot)}. Scanning is complete; I am leaving this source server now.\n\n"
                            f"**Next:** {invite_link(guild.id)} here once so I can check your administrator access. "
                            "Then go to your receiving server and use `/copy`. **Do not run /scan again.**\n"
                            "The attached file contains the full layout and anything that could not be saved.", file=attachment)
            except (discord.HTTPException, discord.RateLimited, aiohttp.ClientError, OSError) as error:
                logging.getLogger(__name__).warning("Snapshot %s saved; notification failed: %s",
                                                    snapshot["id"], type(error).__name__)
            finally:
                # File.close restores a caller-owned BytesIO; close that buffer too.
                attachment.close()
                attachment.fp.close()
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


@bot.tree.command(name="preview", description="See the categories and channels in a saved layout.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.rename(snapshot_id="template")
@app_commands.describe(snapshot_id="Choose a saved layout. Leave blank to use the latest one you can access.")
@app_commands.autocomplete(snapshot_id=snapshot_choices)
async def preview_command(interaction: discord.Interaction, snapshot_id: str | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    snapshot, _ = await source_snapshot(interaction, snapshot_id)
    await reply(interaction, "Use `/copy` in the receiving server when you are ready.", embed=template_embed(snapshot),
                file=text_file(preview(snapshot), "structure.txt"))


class PrivateView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=180)
        self.user_id = user_id

    async def interaction_check(self, interaction):
        if interaction.user.id == self.user_id:
            return True
        await reply(interaction, "This menu belongs to someone else. Use /help to open your own.")
        return False

    async def on_error(self, interaction, error, item):
        await report_error(interaction, error)


class TemplateList(PrivateView):
    def __init__(self, user_id, snapshots, page=0):
        super().__init__(user_id)
        self.snapshots = snapshots
        self.page = page
        shown = snapshots[page * 10:(page + 1) * 10]
        self.choose.options = [discord.SelectOption(label=template_label(item), value=item["id"],
                                description=totals(item)[:100]) for item in shown]
        self.previous.disabled = page == 0
        self.next_page.disabled = (page + 1) * 10 >= len(snapshots)

    def embed(self):
        embed = discord.Embed(title="Your saved layouts", colour=discord.Colour.blurple())
        embed.description = "Choose a layout below to see its channels. To create it here, use `/copy`."
        for item in self.snapshots[self.page * 10:(self.page + 1) * 10]:
            embed.add_field(name=template_label(item), value=f"From {safe(item['source']['name'])} · {totals(item)}", inline=False)
        embed.set_footer(text=f"Page {self.page + 1} of {(len(self.snapshots) + 9) // 10} · Newest first · Only layouts you can administer are shown.")
        return embed

    @discord.ui.select(placeholder="Choose a saved layout to preview", row=0)
    async def choose(self, interaction, select):
        await interaction.response.defer(ephemeral=True, thinking=True)
        snapshot, _ = await source_snapshot(interaction, select.values[0])
        await reply(interaction, "Ready to use it? Run `/copy` in the receiving server and choose this template.",
                    embed=template_embed(snapshot), file=text_file(preview(snapshot), "full-layout.txt"))

    async def turn_page(self, interaction, offset):
        await interaction.response.defer()
        current = await accessible_templates(interaction)
        page = min(max(0, self.page + offset), max(0, (len(current) - 1) // 10))
        if not current:
            await interaction.edit_original_response(content="No layouts are available now. Use /help for the next steps.", embed=None, view=None)
            return
        view = TemplateList(self.user_id, current, page)
        self.stop()
        await interaction.edit_original_response(embed=view.embed(), view=view)

    @discord.ui.button(label="Previous", row=1)
    async def previous(self, interaction, button):
        await self.turn_page(interaction, -1)

    @discord.ui.button(label="Next", row=1)
    async def next_page(self, interaction, button):
        await self.turn_page(interaction, 1)


@bot.tree.command(name="templates", description="See your saved layouts and choose one to preview.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def snapshots_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    snapshots = await accessible_templates(interaction)
    if not snapshots:
        await reply(interaction, "**No saved layouts are available yet.**\nUse `/scan` in the original server first. "
                    "Already scanned? Re-invite me to that server so I can check your access, then retry `/templates` here. Use `/help` for all steps.")
        return
    view = TemplateList(interaction.user.id, snapshots)
    await reply(interaction, None, embed=view.embed(), view=view)


@bot.tree.command(name="delete", description="Delete a saved layout after confirming. Your server's channels stay.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.rename(snapshot_id="template")
@app_commands.describe(snapshot_id="Choose the saved layout you want to delete.")
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
        await reply(click, "Saved layout deleted. Your Discord channels are unchanged. Use /templates to see the remaining layouts.")
    await reply(interaction, f"**Delete the saved layout {safe(template_name(snapshot))}?**\n"
                f"It contains {totals(snapshot)}. This only removes the saved template, not any Discord channels.\n"
                "Choose **Delete saved layout** within two minutes, or Cancel.",
                view=Confirmation(interaction.user.id, action, label="Delete saved layout"))


async def copy_access(interaction, snapshot_id, destination_id):
    destination_id = destination_id or str(interaction.guild_id)
    if (not destination_id.isascii() or not destination_id.isdecimal()
            or not 0 < int(destination_id) < 2**64):
        raise ValueError("Choose a receiving server from the server suggestions, or leave server blank to use this server.")
    if snapshot_id is None:
        available = await accessible_templates(interaction)
        candidates = [item for item in available if item["source"]["id"] != destination_id]
        if not candidates:
            raise ValueError("No layout from another server is available. Use /scan in the original server, "
                             "re-invite me there once, then run /copy in the receiving server. Use /help for the steps.")
        snapshot_id = candidates[0]["id"]
    snapshot, source = await source_snapshot(interaction, snapshot_id)
    destination = await require_admin(interaction.user.id, int(destination_id))
    if destination.id == source.id:
        raise ValueError("Choose a separate receiving server: go to the server where you want the new channels and run /copy there.")
    destination = await require_bot(destination, any(c["overwrites"] for c in snapshot["channels"]))
    return snapshot, destination


@bot.tree.command(name="copy", description="Create a saved layout in this server. Review the plan before anything changes.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.rename(snapshot_id="template", destination_id="server", role_map="roles")
@app_commands.describe(snapshot_id="Choose a saved layout. Leave blank to use the latest one you can access.",
                       destination_id="Usually leave blank: copies into the server you are in.",
                       role_map="Optional advanced mapping to existing roles. Leave blank to create the needed roles automatically.")
@app_commands.autocomplete(snapshot_id=snapshot_choices, destination_id=destination_choices)
async def copy_structure_command(interaction: discord.Interaction, snapshot_id: str | None = None,
                                 destination_id: str | None = None, role_map: str | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    await present_copy_plan(interaction, snapshot_id, destination_id, parse_role_map(role_map))


async def present_copy_plan(interaction, snapshot_id, destination_id, mapping):
    snapshot, destination = await copy_access(interaction, snapshot_id, destination_id)
    # Freeze the resolved template and destination for every subsequent button.
    snapshot_id, destination_id = snapshot["id"], str(destination.id)
    roles = await destination.fetch_roles()
    job = copier.job(snapshot, destination)
    mapping = {**job.get("role_map", {}), **mapping}
    missing = sorted(missing_role_ids(snapshot, mapping))
    blocked = plan(snapshot, destination, roles, mapping, job, pending_roles=missing)
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
        await reply(click, "**Creating your channels…**\nExisting channels will stay. This can take a few minutes. "
                    "I will send a report when finished. If the reply expires, use `/copy` again to see saved progress; it will not start another copy without confirmation.")
    lines = [f"Destination: {destination.name} ({destination.id})",
             f"Will create: {counts(planned)} ({len(new)} entries).",
             f"Already tracked: {len(job['created'])}. Blocked: {len(blocked)}.",
             "Existing channels are kept. Copied entries are appended within Discord's ordering groups.",
             f"Will automatically create {len(missing)} missing roles for channel permissions. New roles have no members or server-wide permissions.",
             "@everyone is mapped automatically. Existing roles are reused only through saved or explicit mappings.",
             "", *[f"BLOCKED {key}: {value}" for key, value in blocked.items()],
             "", preview(snapshot), "", "Previous copy report:", *job.get("report", [])]
    embed = discord.Embed(title="Review your copy", colour=discord.Colour.blurple(),
        description=f"**{safe(template_name(snapshot))}** → **{safe(destination.name)}**\nExisting channels will stay. Nothing is created until you confirm.")
    embed.add_field(name="New channels and categories", value=totals(planned), inline=False)
    embed.add_field(name="Already copied", value=str(len(job["created"])), inline=True)
    embed.add_field(name="Roles created automatically", value=str(len(missing)), inline=True)
    embed.add_field(name="Cannot copy yet", value=str(len(blocked)), inline=True)
    if blocked:
        names = {c["id"]: c["name"] for c in snapshot["channels"]}
        reasons = [f"**{safe(names[key], 70)}:** {blocker_text(reason)}" for key, reason in list(blocked.items())[:4]]
        if len(blocked) > 4:
            reasons.append(f"…and {len(blocked) - 4} more. See the attached plan.")
        embed.add_field(name="What needs fixing", value="\n".join(reasons)[:1024], inline=False)
    embed.add_field(name="Next step", value="Press **Create channels**. Needed roles and channel permissions are copied automatically. Already copied channels are skipped. Buttons expire in two minutes.", inline=False)
    view = Confirmation(interaction.user.id, action, label="Create channels" if new else "Retry saved copy")
    if not new and not job["created"]:
        view.confirm.disabled = True
    if missing and interaction.guild_id == destination.id:
        match = discord.ui.Button(label="Use existing roles (optional)", style=discord.ButtonStyle.secondary)
        async def match_roles(click):
            if view.used:
                await reply(click, "This menu was already used. Run /copy for a fresh plan.")
                return
            view.used = True
            for child in view.children:
                child.disabled = True
            view.stop()
            await click.response.edit_message(content="Opening the permission picker. No channels have been created.", view=view)
            current, source = await source_snapshot(click, snapshot_id)
            if fingerprint(current) != digest:
                raise ValueError("The layout changed. Use /copy to review it again.")
            names = {str(role.id): role.name for role in await source.fetch_roles()}
            wizard = RoleMatcher(click.user.id, snapshot_id, destination_id, mapping, missing, names)
            await reply(click, wizard.message(), view=wizard)
        async def guarded_match(click):
            try:
                await match_roles(click)
            except Exception as error:
                await report_error(click, error)
        match.callback = guarded_match
        view.add_item(match)
    await reply(interaction, None, embed=embed, file=text_file("\n".join(lines), "copy-plan.txt"), view=view)


class RoleMatcher(PrivateView):
    def __init__(self, user_id, snapshot_id, destination_id, mapping, missing, names, index=0):
        super().__init__(user_id)
        self.snapshot_id, self.destination_id = snapshot_id, destination_id
        self.mapping, self.missing, self.names = dict(mapping), list(missing), names
        self.index = index
        self.busy = False
        self.used = False

    def message(self):
        source_id = self.missing[self.index]
        name = safe(self.names.get(source_id, f"Original role {source_id}"), 120)
        return (f"**Match permissions — role {self.index + 1} of {len(self.missing)}**\n"
                f"The original server uses **{name}**. Which role should have the same channel access here?\n"
                "Choose an existing role below. Create it in Server Settings → Roles first if needed. "
                "Role names do not have to match; check who has the role before choosing.\n"
                "**Create remaining roles** uses automatic role creation for the rest. No channels are created in this step.")

    async def apply_role(self, interaction, selected_id):
        if self.used or self.busy:
            await reply(interaction, "This menu is already processing or finished. Use /copy for a fresh plan.")
            return
        self.busy = True
        try:
            await interaction.response.defer(ephemeral=True)
            if interaction.guild_id != int(self.destination_id):
                raise ValueError("Use the role picker in the receiving server.")
            destination = await require_admin(interaction.user.id, int(self.destination_id))
            await source_snapshot(interaction, self.snapshot_id)
            roles = {str(role.id): role for role in await destination.fetch_roles()}
            role = roles.get(str(selected_id))
            if role is None or role.managed or role.id == destination.id:
                raise ValueError("Choose a normal role you created, rather than @everyone or a bot/integration role.")
            if str(role.id) in self.mapping.values():
                raise ValueError("That role is already matched. Choose a different role for this original role.")
            self.mapping[self.missing[self.index]] = str(role.id)
            self.index += 1
            if self.index == len(self.missing):
                self.used = True
                self.stop()
                await interaction.edit_original_response(content="Roles matched. Review the updated copy plan below.", view=None)
                await present_copy_plan(interaction, self.snapshot_id, self.destination_id, self.mapping)
            else:
                # A fresh view makes queued clicks from the previous role single-use.
                self.used = True
                self.stop()
                next_view = RoleMatcher(self.user_id, self.snapshot_id, self.destination_id,
                                        self.mapping, self.missing, self.names, self.index)
                await interaction.edit_original_response(content=next_view.message(), view=next_view)
        finally:
            self.busy = False

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Choose the matching role in this server", row=0)
    async def choose_role(self, interaction, select):
        await self.apply_role(interaction, select.values[0].id)

    @discord.ui.button(label="Create remaining roles", row=1)
    async def skip_remaining(self, interaction, button):
        if self.used or self.busy:
            await reply(interaction, "This menu was already used. Run /copy for a fresh plan.")
            return
        self.used = True
        self.stop()
        await interaction.response.edit_message(content="Remaining roles will be created automatically. Press Create channels in the plan below.", view=None)
        await present_copy_plan(interaction, self.snapshot_id, self.destination_id, self.mapping)

    @discord.ui.button(label="Cancel", row=1)
    async def cancel_matching(self, interaction, button):
        if self.used or self.busy:
            await reply(interaction, "This menu was already used. Run /copy for a fresh plan.")
            return
        self.used = True
        self.stop()
        await interaction.response.edit_message(content="Cancelled. No channels were created. Use /copy when you are ready.", view=None)


async def run_copy(interaction, snapshot, destination, mapping):
    try:
        names = {}
        if missing_role_ids(snapshot, {**copier.job(snapshot, destination)["role_map"], **mapping}):
            source = bot.get_guild(int(snapshot["source"]["id"]))
            if source is not None:
                names = {str(role.id): role.name for role in await source.fetch_roles()}
        report = await copier.copy(snapshot, destination, mapping,
                                   f"Structure template {snapshot['id']}", automatic_role_names=names)
        try:
            title, message = report_summary(report)
            await reply(interaction, f"**{title}**\n{message}",
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
        message = error_text(str(error))
    elif isinstance(error, FileNotFoundError):
        message = "I could not find that saved layout. Use /templates to see what is available, then choose one by name."
    elif isinstance(error, app_commands.AppCommandError):
        message = "This command requires a server Administrator."
    elif isinstance(error, discord.InvalidData):
        message = "Discord returned a channel type I cannot read yet. Nothing was saved. Ask the bot owner to update CAPS, then try /scan again."
    elif isinstance(error, (discord.HTTPException, discord.RateLimited)):
        if isinstance(error, discord.Forbidden):
            message = "Discord did not allow that action. Check that CAPS has View Channels, Manage Channels and Manage Roles. "
            message += "Private channels may also need access. Any completed copy progress is saved. Use /help for setup steps."
        elif isinstance(error, discord.RateLimited) or error.status == 429:
            message = "Discord asked me to slow down. Wait a little, then retry the same command. Any completed copy progress is saved."
        else:
            message = "Discord could not finish that request. Try again shortly. Any completed copy progress is saved. " + api_error(error)
    else:
        message = f"I could not finish that step ({type(error).__name__}). Any saved layout or copy progress is still kept. "
        message += "Ask the person running the bot to check it before retrying if this keeps happening."
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
