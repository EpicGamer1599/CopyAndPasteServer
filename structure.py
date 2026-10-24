"""Snapshot model and resumable, append-only Discord channel copying."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import asyncio
import hashlib
import json
import uuid

import aiohttp
import discord

from safe_http import guarded_create
from storage import Store

SUPPORTED = {0, 2, 4, 5, 13, 15, 16}
TYPE_NAMES = {0: "text", 2: "voice", 4: "category", 5: "announcement",
              13: "stage", 15: "forum", 16: "media"}
COMMON = ("topic", "slowmode_delay", "nsfw", "default_auto_archive_duration",
          "default_thread_slowmode_delay", "bitrate", "user_limit", "rtc_region")


def bucket(channel_type: int) -> int:
    return 4 if channel_type == 4 else 2 if channel_type in (2, 13) else 0


def ordered_channels(channels):
    """Match Guild.by_category UI ordering, using fresh REST channel objects."""
    categories = sorted((c for c in channels if c.type.value == 4),
                        key=lambda c: (c.position, c.id))
    known = {c.id for c in categories}
    children = [c for c in channels if c.type.value != 4]
    def siblings(parent):
        return sorted((c for c in children if c.category_id == parent),
                      key=lambda c: (bucket(c.type.value), c.position, c.id))
    result = siblings(None)
    for category in categories:
        result.append(category)
        result.extend(siblings(category.id))
    # Preserve inaccessible-parent references, never silently uncategorize them.
    result.extend(sorted((c for c in children if c.category_id is not None
                          and c.category_id not in known),
                         key=lambda c: (bucket(c.type.value), c.position, c.id)))
    return result


def capture(guild, channels) -> dict:
    visible = [c for c in channels if c.permissions_for(guild.me).view_channel]
    snapshot = {
        "version": 1, "id": uuid.uuid4().hex,
        "source": {"id": str(guild.id), "name": guild.name},
        "created_at": datetime.now(timezone.utc).isoformat(), "channels": [],
        "limitations": [
            "Only channels visible to the bot were captured; client-specific views are unavailable.",
            "Threads/posts, messages, members, webhooks, invites and live stage sessions are excluded.",
            "Role definitions, guild settings, integrations and channel followers are not copied.",
            "Names/topics are template content; review them locally for sensitive text before sharing.",
            "Member overwrites are omitted without identifiers; affected channels are blocked from copying.",
            "Custom emoji assets/IDs and forum tag IDs cannot be transferred; tags get new IDs.",
        ],
    }
    for c in ordered_channels(visible):
        kind = c.type.value
        entry = {"id": str(c.id), "name": c.name, "type": kind,
                 "position": c.position,
                 "parent_id": str(c.category_id) if c.category_id else None,
                 "settings": {}, "overwrites": [], "omitted": [],
                 "member_overwrites_omitted": False}
        for target, overwrite in c.overwrites.items():
            is_role = isinstance(target, discord.Role) or (
                isinstance(target, discord.Object) and target.type is discord.Role)
            if not is_role:
                entry["member_overwrites_omitted"] = True
                continue
            allow, deny = overwrite.pair()
            entry["overwrites"].append({"role_id": str(target.id),
                                        "everyone": target.id == guild.id,
                                        "allow": allow.value, "deny": deny.value})
        if entry["member_overwrites_omitted"]:
            entry["omitted"].append("Member-specific overwrites: copy blocked for privacy and access safety.")
        for name in COMMON:
            if hasattr(c, name):
                entry["settings"][name] = getattr(c, name)
        if kind == 13:
            entry["settings"].pop("topic", None)
            entry["omitted"].append("Stage topic is session data and is not recreated.")
        if hasattr(c, "video_quality_mode"):
            entry["settings"]["video_quality_mode"] = c.video_quality_mode.value
        if kind in (15, 16):
            entry["settings"].update(
                default_sort_order=c.default_sort_order.value if c.default_sort_order else None,
                default_layout=c.default_layout.value,
                require_tag=c.flags.require_tag,
            )
            emoji = c.default_reaction_emoji
            entry["settings"]["default_reaction_emoji"] = (
                str(emoji) if emoji and not emoji.id else None)
            if emoji and emoji.id:
                entry["omitted"].append("Custom default reaction emoji omitted.")
            tags = []
            for tag in c.available_tags:
                tags.append({"name": tag.name, "moderated": tag.moderated,
                             "emoji": str(tag.emoji) if tag.emoji and not tag.emoji.id else None})
                if tag.emoji and tag.emoji.id:
                    entry["omitted"].append(f"Custom emoji omitted for tag {tag.name}.")
            entry["settings"]["available_tags"] = tags
            if kind == 16:
                entry["omitted"].append("Media channels use Discord's fixed layout; default forum layout cannot be applied.")
        if hasattr(c, "flags"):
            flags = c.flags.value & ~discord.ChannelFlags.VALID_FLAGS["require_tag"]
            if flags:
                entry["omitted"].append(f"Additional channel flags ({flags}) are not recreated by this bot.")
        if kind not in SUPPORTED:
            entry["omitted"].append(f"Unsupported channel type {kind}; copy blocked.")
        snapshot["channels"].append(entry)
    return snapshot


def validate_snapshot(snapshot):
    """Reject damaged template shapes before any Discord write or metadata display."""
    def snowflake(value):
        return (isinstance(value, str) and value.isascii() and value.isdecimal()
                and 0 < int(value) < 2**64 and str(int(value)) == value)

    def need(condition):
        if not condition:
            raise ValueError("Invalid snapshot field")

    try:
        need(isinstance(snapshot, dict) and type(snapshot["version"]) is int and snapshot["version"] == 1)
        need(isinstance(snapshot["id"], str) and len(snapshot["id"]) == 32)
        need(all(c in "0123456789abcdef" for c in snapshot["id"]))
        need(snowflake(snapshot["source"]["id"]))
        need(isinstance(snapshot["source"]["name"], str))
        need(isinstance(snapshot["created_at"], str))
        datetime.fromisoformat(snapshot["created_at"])
        need(isinstance(snapshot["limitations"], list))
        need(all(isinstance(v, str) for v in snapshot["limitations"]))
        need(isinstance(snapshot["channels"], list))
        seen = set()
        for entry in snapshot["channels"]:
            need(snowflake(entry["id"]) and entry["id"] not in seen)
            seen.add(entry["id"])
            need(isinstance(entry["name"], str) and 1 <= len(entry["name"]) <= 100)
            need(type(entry["type"]) is int and type(entry["position"]) is int)
            need(entry["parent_id"] is None or snowflake(entry["parent_id"]))
            need(entry["type"] != 4 or entry["parent_id"] is None)
            need(type(entry["member_overwrites_omitted"]) is bool)
            need(isinstance(entry["settings"], dict))
            settings = entry["settings"]
            text_settings = {"topic", "slowmode_delay", "nsfw", "default_auto_archive_duration",
                             "default_thread_slowmode_delay"}
            voice_settings = {"slowmode_delay", "nsfw", "bitrate", "user_limit", "rtc_region",
                              "video_quality_mode"}
            forum_settings = text_settings | {"default_sort_order", "default_layout", "require_tag",
                                               "default_reaction_emoji", "available_tags"}
            allowed = ({4: {"nsfw"}, 0: text_settings, 5: text_settings,
                        2: voice_settings, 13: voice_settings, 15: forum_settings, 16: forum_settings}
                       .get(entry["type"], set(COMMON)))
            need(set(settings) <= allowed)
            for key in ("slowmode_delay", "default_auto_archive_duration", "default_thread_slowmode_delay",
                        "bitrate", "user_limit", "video_quality_mode", "default_layout"):
                if key in settings:
                    need(type(settings[key]) is int and settings[key] >= 0)
            for key in ("nsfw", "require_tag"):
                if key in settings:
                    need(type(settings[key]) is bool)
            for key in ("topic", "rtc_region", "default_reaction_emoji"):
                if key in settings:
                    need(settings[key] is None or isinstance(settings[key], str))
            if "default_sort_order" in settings:
                need(settings["default_sort_order"] is None or settings["default_sort_order"] in (0, 1))
            if "video_quality_mode" in settings:
                need(settings["video_quality_mode"] in (1, 2))
            if entry["type"] in (15, 16):
                need(settings.get("default_layout") in (0, 1, 2))
                need(type(settings.get("require_tag")) is bool)
                need(isinstance(settings.get("available_tags"), list))
                for tag in settings["available_tags"]:
                    need(isinstance(tag["name"], str) and 1 <= len(tag["name"]) <= 20)
                    need(type(tag["moderated"]) is bool)
                    need(tag["emoji"] is None or isinstance(tag["emoji"], str))
            need(isinstance(entry["omitted"], list))
            need(all(isinstance(v, str) for v in entry["omitted"]))
            need(isinstance(entry["overwrites"], list))
            targets = set()
            for overwrite in entry["overwrites"]:
                need(snowflake(overwrite["role_id"]) and overwrite["role_id"] not in targets)
                targets.add(overwrite["role_id"])
                need(type(overwrite["everyone"]) is bool)
                need(overwrite["everyone"] == (overwrite["role_id"] == snapshot["source"]["id"]))
                for field in ("allow", "deny"):
                    need(type(overwrite[field]) is int and overwrite[field] >= 0)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("Malformed snapshot; restore it from a backup or capture a new one.") from None


def counts(snapshot) -> str:
    kinds = Counter(TYPE_NAMES.get(c["type"], f"unknown({c['type']})")
                    for c in snapshot["channels"])
    return ", ".join(f"{count} {name}" for name, count in sorted(kinds.items())) or "empty"


def snapshot_digest(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


def preview(snapshot) -> str:
    lines = [f"Snapshot {snapshot['id']}", f"Source: {snapshot['source']['name']}",
             f"Created: {snapshot['created_at']}", counts(snapshot),
             f"Total: {sum(c['type'] == 4 for c in snapshot['channels'])} categories, "
             f"{sum(c['type'] != 4 for c in snapshot['channels'])} channels.", "", "Uncategorized:"]
    known = {c["id"] for c in snapshot["channels"] if c["type"] == 4}
    for c in snapshot["channels"]:
        if c["type"] == 4:
            lines.append(f"\n[Category] {c['name']} ({c['id']})")
        else:
            parent = c["parent_id"]
            prefix = "  " if parent in known else ""
            suffix = " [parent unavailable]" if parent and parent not in known else ""
            lines.append(f"{prefix}- {c['name']} [{TYPE_NAMES.get(c['type'], c['type'])}] ({c['id']}){suffix}")
        lines.extend(f"    OMITTED: {warning}" for warning in c["omitted"])
    lines.extend(["", "Capture limitations:", *snapshot["limitations"]])
    return "\n".join(lines)


def parse_role_map(value: str | None) -> dict[str, str]:
    mapping = {}
    if not value:
        return mapping
    for pair in value.split(","):
        source, separator, destination = pair.strip().partition(":")
        source, destination = source.strip(), destination.strip()
        if (not separator or any(not v.isascii() or not v.isdecimal()
                                 or not 0 < int(v) < 2**64 or str(int(v)) != v
                                 for v in (source, destination))):
            raise ValueError("Use role_map source_role_id:destination_role_id,source_role_id:destination_role_id")
        if source in mapping or destination in mapping.values():
            raise ValueError("Each source and destination role may appear only once.")
        mapping[source] = destination
    return mapping


def map_overwrites(entry, guild, roles, role_map):
    if entry["member_overwrites_omitted"]:
        raise ValueError("Member-specific overwrites were omitted; channel blocked.")
    by_id = {str(r.id): r for r in roles}
    result = {}
    for overwrite in entry["overwrites"]:
        if overwrite["everyone"]:
            role = by_id.get(str(guild.id))
        else:
            role = by_id.get(role_map.get(overwrite["role_id"]))
            if role is None or role.id == guild.id or role.managed:
                raise ValueError(f"Missing/unsafe mapping for source role {overwrite['role_id']}.")
        if role is None or role in result:
            raise ValueError("Missing or colliding destination role.")
        result[role] = discord.PermissionOverwrite.from_pair(
            discord.Permissions(overwrite["allow"]), discord.Permissions(overwrite["deny"]))
    return result


def validate_bot_permissions(guild, overwrites=False):
    permissions = guild.me.guild_permissions
    if not permissions.view_channel or not permissions.manage_channels:
        raise ValueError("Bot requires View Channels and Manage Channels in the destination.")
    if overwrites and not permissions.manage_roles:
        raise ValueError("Bot requires Manage Roles to apply permission overwrites.")


def plan(snapshot, guild, roles, role_map, job):
    """Return per-entry blockers. No Discord mutations happen in this function."""
    if job.get("pending"):
        raise ValueError("This copy has an uncertain create request. Resolve its pending journal entry locally before resuming.")
    if job.get("snapshot_digest") and job["snapshot_digest"] != snapshot_digest(snapshot):
        raise ValueError("Snapshot changed since this copy began; restore the original snapshot.")
    for source, destination in job.get("role_map", {}).items():
        if role_map.get(source) != destination:
            raise ValueError("Existing copy mappings cannot be changed or removed; supply the previous mappings too.")
    validate_snapshot(snapshot)
    validate_bot_permissions(guild, any(c["overwrites"] for c in snapshot["channels"]))
    blocked = {}
    categories = {c["id"] for c in snapshot["channels"] if c["type"] == 4}
    # Parent validation is independent of snapshot list traversal order.
    for c in snapshot["channels"]:
        try:
            if c["type"] not in SUPPORTED:
                raise ValueError(f"Unsupported channel type {c['type']}.")
            if c["type"] in (5, 13, 15, 16) and "COMMUNITY" not in guild.features:
                raise ValueError("Destination requires Community for this channel type.")
            if c["parent_id"] and c["parent_id"] not in categories:
                raise ValueError("Source parent category was inaccessible.")
            if c["settings"].get("bitrate", 0) > guild.bitrate_limit:
                raise ValueError("Source bitrate exceeds destination bitrate limit.")
            map_overwrites(c, guild, roles, role_map)
        except ValueError as error:
            blocked[c["id"]] = str(error)
    for c in snapshot["channels"]:
        if c["parent_id"] in blocked:
            blocked[c["id"]] = "Parent category is blocked."
    return blocked


async def create_channel(guild, entry, parent, overwrites, reason):
    kind = entry["type"]
    settings = dict(entry["settings"])
    options = {"overwrites": overwrites, "reason": reason}
    if kind == 4:
        return await guild.create_category(entry["name"], **options)
    options["category"] = parent
    if kind in (0, 5):
        options.update(settings)
        return await guild.create_text_channel(entry["name"], news=kind == 5, **options)
    if kind in (2, 13):
        settings.pop("slowmode_delay", None)
        if "video_quality_mode" in settings:
            settings["video_quality_mode"] = discord.VideoQualityMode(settings["video_quality_mode"])
        options.update(settings)
        creator = guild.create_stage_channel if kind == 13 else guild.create_voice_channel
        channel = await creator(entry["name"], **options)
        # Voice slowmode is applied separately after the durable ID checkpoint.
        return channel
    if kind in (15, 16):
        settings.pop("require_tag", None)
        if settings.get("default_reaction_emoji") is None:
            settings.pop("default_reaction_emoji", None)
        settings["available_tags"] = [discord.ForumTag(**tag) for tag in settings["available_tags"]]
        if settings.get("default_sort_order") is not None:
            settings["default_sort_order"] = discord.ForumOrderType(settings["default_sort_order"])
        else:
            settings.pop("default_sort_order", None)
        settings["default_layout"] = discord.ForumLayoutType(settings["default_layout"])
        options.update(settings)
        return await guild.create_forum(entry["name"], media=kind == 16, **options)
    raise ValueError("Unsupported channel type.")


def api_error(error):
    # Deliberately do not echo HTTP bodies, tokens, request headers or URLs.
    if isinstance(error, discord.HTTPException):
        return f"Discord HTTP {error.status}, code {error.code}"
    if isinstance(error, discord.RateLimited):
        return "Discord rate limit exceeded; retry later."
    return type(error).__name__


async def reapply_positions(guild, snapshot, created, current, reason):
    """One official bulk-position request. Never includes parent changes.

    discord.py 2.7 has no public bulk-position method. This small compatibility
    boundary uses its HTTP client (and therefore its normal rate-limit handling).
    Existing channels remain in relative order, followed by new ones per bucket.
    Only copied IDs are submitted; unrelated channels are not explicitly edited.
    """
    copied = set(created.values())
    payload = []
    for group in (0, 2, 4):
        existing = sorted((c for c in current if c.id not in copied and bucket(c.type.value) == group),
                          key=lambda c: (c.position, c.id))
        new_ids = [created[c["id"]] for c in snapshot["channels"]
                   if c["id"] in created and bucket(c["type"]) == group]
        offset = max((c.position for c in existing), default=-1) + 1
        payload.extend({"id": channel_id, "position": offset + position}
                       for position, channel_id in enumerate(new_ids))
    if payload:
        await guild._state.http.bulk_channel_update(guild.id, payload, reason=reason)


class Copier:
    def __init__(self, store: Store):
        self.store = store
        self.locks: dict[int, asyncio.Lock] = {}

    def key(self, snapshot, guild):
        return f"{snapshot['id']}_{guild.id}"

    def job(self, snapshot, guild):
        try:
            job = self.store.read("jobs", self.key(snapshot, guild))
            valid = (job.get("snapshot_id") == snapshot["id"]
                     and job.get("destination_id") == str(guild.id)
                     and (job.get("pending") is None or isinstance(job.get("pending"), str))
                     and isinstance(job.get("created"), dict)
                     and isinstance(job.get("role_map"), dict)
                     and all(isinstance(k, str) and isinstance(v, str) for k, v in job["role_map"].items())
                     and isinstance(job.get("report"), list)
                     and all(isinstance(line, str) for line in job["report"]))
            if not valid or any(not isinstance(key, str) or type(value) is not int or value <= 0
                                for key, value in job["created"].items()):
                raise ValueError("Malformed copy journal; restore or reconcile it before resuming.")
            return job
        except FileNotFoundError:
            return {"snapshot_id": snapshot["id"], "destination_id": str(guild.id),
                    "created": {}, "pending": None, "role_map": {}, "report": []}

    async def copy(self, snapshot, guild, role_map, reason):
        lock = self.locks.setdefault(guild.id, asyncio.Lock())
        if lock.locked():
            raise ValueError("Another snapshot or copy in this destination is running; retry later.")
        async with lock:
            roles = await guild.fetch_roles()
            guild._roles = {role.id: role for role in roles}
            job = self.job(snapshot, guild)
            blocked = plan(snapshot, guild, roles, role_map, job)
            current = {c.id: c for c in await guild.fetch_channels()}
            for source_id, destination_id in job["created"].items():
                if destination_id not in current:
                    raise ValueError(f"Previously created channel {destination_id} is missing. Restore it or resolve the journal; no automatic duplicate was made.")
            # Never edit or use an externally moved/retyped channel as a parent.
            entries = {entry["id"]: entry for entry in snapshot["channels"]}
            if len(set(job["created"].values())) != len(job["created"]):
                raise ValueError("Copy journal contains colliding destination channel IDs.")
            for source_id, destination_id in job["created"].items():
                entry = entries.get(source_id)
                channel = current[destination_id]
                if (entry is None or channel.type.value != entry["type"]
                        or channel.category_id != job["created"].get(entry["parent_id"])):
                    raise ValueError("Previously created channel type or parent changed; resolve the journal before resuming.")
            job["role_map"] = role_map
            job["snapshot_digest"] = snapshot_digest(snapshot)
            report = [f"{c['id']} {c['name']}: {warning}" for c in snapshot["channels"]
                      for warning in c["omitted"]]
            report.extend(snapshot["limitations"])
            def save():
                job["report"] = report
                self.store.write("jobs", self.key(snapshot, guild), job)
            save()
            for c in snapshot["channels"]:
                source_id = c["id"]
                if source_id in blocked:
                    report.append(f"BLOCKED {source_id} {c['name']}: {blocked[source_id]}")
                    continue
                parent_id = c["parent_id"]
                if parent_id and parent_id not in job["created"]:
                    report.append(f"BLOCKED {source_id}: Parent was not created.")
                    continue
                if source_id not in job["created"]:
                    parent = current.get(job["created"].get(parent_id))
                    job["pending"] = source_id
                    save()  # write-ahead: a crash after this point must not blindly retry
                    try:
                        with guarded_create():
                            channel = await create_channel(
                                guild, c, parent, map_overwrites(c, guild, roles, role_map), reason)
                    except asyncio.CancelledError:
                        report.append(f"INTERRUPTED {source_id}: outcome uncertain; journal reconciliation required")
                        save()
                        raise
                    except (discord.HTTPException, discord.RateLimited, aiohttp.ClientError,
                            OSError, TimeoutError) as error:
                        # 4xx (apart from request timeouts) are definite rejections.
                        definite = (isinstance(error, discord.HTTPException)
                                    and 400 <= error.status < 500 and error.status != 408)
                        if definite or isinstance(error, discord.RateLimited):
                            job["pending"] = None
                        report.append(f"FAILED {source_id}: {api_error(error)}; "
                                      + ("safe to resume" if not job["pending"] else "outcome uncertain; journal reconciliation required"))
                        save()
                        break
                    except Exception as error:
                        # Includes a malformed/unknown API response after a successful POST.
                        # Keep pending: the server may already have created the channel.
                        report.append(f"FAILED {source_id}: {api_error(error)}; outcome uncertain; journal reconciliation required")
                        save()
                        break
                    job["created"][source_id] = channel.id
                    job["pending"] = None
                    current[channel.id] = channel
                    save()
                channel = current[job["created"][source_id]]
                # Idempotent settings edits are safe to repeat on resume.
                try:
                    if c["type"] == 4 and "nsfw" in c["settings"]:
                        await channel.edit(nsfw=c["settings"]["nsfw"], reason=reason)
                    if c["type"] in (2, 13) and "slowmode_delay" in c["settings"]:
                        await channel.edit(slowmode_delay=c["settings"]["slowmode_delay"], reason=reason)
                    if c["type"] in (15, 16):
                        await channel.edit(require_tag=c["settings"]["require_tag"], reason=reason)
                except (discord.HTTPException, discord.RateLimited, aiohttp.ClientError,
                        OSError, TimeoutError) as error:
                    report.append(f"SETTING FAILED {source_id}: {api_error(error)}; rerun to retry settings.")
                save()
            if job["created"]:
                try:
                    channels = await guild.fetch_channels()
                    await reapply_positions(guild, snapshot, job["created"], channels, reason)
                    actual = {str(c.id): c for c in await guild.fetch_channels()}
                    actual_order = [str(c.id) for c in ordered_channels(list(actual.values()))
                                    if c.id in set(job["created"].values())]
                    expected = [str(job["created"][c["id"]]) for c in snapshot["channels"]
                                if c["id"] in job["created"]]
                    if actual_order != expected:
                        report.append("ORDER MISMATCH: server layout changed or Discord normalized positions. Rerun to retry.")
                    for c in snapshot["channels"]:
                        if c["id"] in job["created"]:
                            channel = actual.get(str(job["created"][c["id"]]))
                            expected_parent = job["created"].get(c["parent_id"])
                            if channel is None or channel.category_id != expected_parent:
                                report.append(f"PARENT MISMATCH {c['id']}: destination was edited; inspect manually.")
                except (discord.HTTPException, discord.RateLimited, aiohttp.ClientError,
                        OSError, TimeoutError) as error:
                    report.append(f"POSITION/VERIFY FAILED: {api_error(error)}; rerun to retry without recreating channels.")
            report.insert(0, f"Created/tracked {len(job['created'])}/{len(snapshot['channels'])} entries. Existing channels were not deleted.")
            save()
            return "\n".join(report)
