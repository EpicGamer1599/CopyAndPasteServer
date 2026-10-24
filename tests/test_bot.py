from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

import main
from storage import Store, acquire_process_lock
from structure import (Copier, capture, counts, map_overwrites, ordered_channels,
                       parse_role_map, plan, preview, snapshot_digest)


def role(role_id, permissions=0, managed=False):
    return {"id": str(role_id), "name": "same-name", "permissions": str(permissions),
            "position": 1, "managed": managed, "color": 0}


def raw(channel_id, kind=0, position=0, parent=None, **settings):
    return {"id": str(channel_id), "type": kind, "name": f"channel-{channel_id}",
            "position": position, "parent_id": str(parent) if parent else None,
            "permission_overwrites": [], "bitrate": 64000, "user_limit": 0, **settings}


class FakeHTTP:
    """Network boundary only: all channel serialization is real discord.py code."""
    def __init__(self, channels, roles):
        self.channels = copy.deepcopy(channels)
        self.roles = copy.deepcopy(roles)
        self.calls = []
        self.failure = None
        self.fail_after = 0
        self.position_failure = None
        self.edit_failure = None
        self.position_payloads = []
        self.role_calls = []
        self.role_failure = None
        self.role_fail_after = 0

    async def get_roles(self, guild_id):
        return copy.deepcopy(self.roles)

    async def create_role(self, guild_id, **kwargs):
        if self.role_failure is not None and len(self.role_calls) >= self.role_fail_after:
            error, self.role_failure = self.role_failure, None
            raise error
        self.role_calls.append(copy.deepcopy(kwargs))
        role_id = max([int(item["id"]) for item in self.roles] + [2000]) + 1
        payload = role(role_id)
        payload.update({key: value for key, value in kwargs.items() if key != "reason"})
        self.roles.append(payload)
        return copy.deepcopy(payload)

    async def get_all_guild_channels(self, guild_id):
        return copy.deepcopy(self.channels)

    async def create_channel(self, guild_id, kind, **kwargs):
        if self.failure is not None and len(self.calls) >= self.fail_after:
            error, self.failure = self.failure, None
            raise error
        self.calls.append((kind, copy.deepcopy(kwargs)))
        channel_id = max([int(c["id"]) for c in self.channels] + [1000]) + 1
        payload = raw(channel_id, kind, len(self.channels))
        payload.update({k: v for k, v in kwargs.items() if k != "reason"})
        for index, tag in enumerate(payload.get("available_tags", [])):
            tag["id"] = str(9000 + index)
        self.channels.append(payload)
        return copy.deepcopy(payload)

    async def edit_channel(self, channel_id, **kwargs):
        if self.edit_failure:
            error, self.edit_failure = self.edit_failure, None
            raise error
        channel = next(c for c in self.channels if int(c["id"]) == channel_id)
        channel.update({k: v for k, v in kwargs.items() if k != "reason"})
        return copy.deepcopy(channel)

    async def bulk_channel_update(self, guild_id, payload, **kwargs):
        self.position_payloads.append(copy.deepcopy(payload))
        if self.position_failure:
            error, self.position_failure = self.position_failure, None
            raise error
        by_id = {int(c["id"]): c for c in self.channels}
        for change in payload:
            by_id[int(change["id"])]["position"] = change["position"]


def guild(guild_id=100, channels=None, roles=None):
    client = discord.Client(intents=discord.Intents.none())
    state = client._connection
    state.user = discord.ClientUser(state=state, data={"id": "9", "username": "bot", "discriminator": "0", "avatar": None})
    roles = roles or [role(guild_id, 8)]
    data = {"id": str(guild_id), "name": f"Server {guild_id}", "owner_id": "42",
            "features": ["COMMUNITY"], "roles": roles, "channels": channels or []}
    result = discord.Guild(data=data, state=state)
    member = discord.Member(data={"user": {"id": "9", "username": "bot", "discriminator": "0", "avatar": None},
                                  "roles": [], "flags": 0}, guild=result, state=state)
    result._add_member(member)
    http = FakeHTTP(channels or [], roles)
    state.http = http
    return result, http


def http_error(status=403):
    return discord.HTTPException(SimpleNamespace(status=status, reason="test"),
                                 {"code": 50013, "message": "deliberately hidden details"})


class OrderingTests(unittest.TestCase):
    def test_ui_order_matches_library_with_categories_and_nested_channels(self):
        channels = [raw(50, 4, 3), raw(40, 4, 1), raw(90, 2, 0, 40),
                    raw(72, 0, 2, 40), raw(71, 0, 2, 40), raw(80, 15, 0, 50),
                    raw(82, 13, 2, 50), raw(11, 2, 0), raw(12, 0, 9)]
        source, _ = guild(channels=channels)
        expected = [12, 11, 40, 71, 72, 90, 50, 80, 82]
        self.assertEqual([c.id for c in ordered_channels(source.channels)], expected)
        library = []
        for category, children in source.by_category():
            if category:
                library.append(category.id)
            library.extend(c.id for c in children)
        self.assertEqual(library, expected)
        snapshot = capture(source, source.channels)
        self.assertEqual([c["id"] for c in snapshot["channels"]], list(map(str, expected)))
        self.assertEqual(snapshot["channels"][3]["parent_id"], "40")
        self.assertIsNone(snapshot["channels"][0]["parent_id"])

    def test_preview_includes_empty_categories_and_totals(self):
        source, _ = guild(channels=[raw(1), raw(2, 4)])
        text = preview(capture(source, source.channels))
        self.assertLess(text.index("Uncategorized:"), text.index("[Category]"))
        self.assertIn("1 categories, 1 channels", text)
        self.assertIn("channel-1 [text]", text)

    def test_stage_topic_is_recorded_as_untransferable(self):
        source, _ = guild(channels=[raw(1, 13, topic="live session")])
        snapshot = capture(source, source.channels)
        self.assertNotIn("topic", snapshot["channels"][0]["settings"])
        self.assertIn("Stage topic", " ".join(snapshot["channels"][0]["omitted"]))

    def test_hidden_channels_are_excluded(self):
        source, _ = guild(channels=[raw(1), raw(2, 4)])
        hidden = SimpleNamespace(permissions_for=lambda member: discord.Permissions.none())
        self.assertEqual(len(capture(source, [*source.channels, hidden])["channels"]), 2)

    def test_uncached_member_overwrite_has_no_identifiers(self):
        source, _ = guild(channels=[raw(1, permission_overwrites=[
            {"id": "987654321", "type": 1, "allow": "0", "deny": "1024"}])])
        snapshot = capture(source, source.channels)
        self.assertNotIn("987654321", json.dumps(snapshot))
        self.assertTrue(snapshot["channels"][0]["member_overwrites_omitted"])


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.source, _ = guild(channels=[raw(1, permission_overwrites=[
            {"id": "100", "type": 0, "allow": "0", "deny": "1024"},
            {"id": "110", "type": 0, "allow": "1024", "deny": "2048"}])],
            roles=[role(100, 8), role(110)])
        self.entry = capture(self.source, self.source.channels)["channels"][0]
        self.destination, _ = guild(200, roles=[role(200, 8), role(110), role(210), role(220, managed=True)])

    def test_everyone_maps_to_destination_and_other_roles_explicitly(self):
        mapped = map_overwrites(self.entry, self.destination, self.destination.roles, {"110": "210"})
        self.assertEqual({r.id for r in mapped}, {200, 210})
        overwrite = next(ow for r, ow in mapped.items() if r.id == 210)
        self.assertTrue(overwrite.view_channel)
        self.assertFalse(overwrite.send_messages)

    def test_same_id_or_name_never_implies_a_mapping(self):
        with self.assertRaisesRegex(ValueError, "mapping"):
            map_overwrites(self.entry, self.destination, self.destination.roles, {})

    def test_managed_missing_and_everyone_role_targets_rejected(self):
        for target in ("220", "999", "200"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                map_overwrites(self.entry, self.destination, self.destination.roles, {"110": target})

    def test_member_omission_blocks_copy(self):
        self.entry["member_overwrites_omitted"] = True
        with self.assertRaisesRegex(ValueError, "Member-specific"):
            map_overwrites(self.entry, self.destination, self.destination.roles, {"110": "210"})

    def test_role_map_parser(self):
        self.assertEqual(parse_role_map("110:210, 120:220"), {"110": "210", "120": "220"})
        for value in ("abc", "1:2,1:3", "1:2,3:2", "1:", "1:2,"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_role_map(value)


class CopyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.copier = Copier(self.store)
        self.source, _ = guild(channels=[raw(1), raw(2, 4, nsfw=True),
                                                 raw(3, 0, 0, 2), raw(4, 2, 0, 2)])
        self.snapshot = capture(self.source, self.source.channels)
        self.destination, self.http = guild(200, channels=[raw(500, 4), raw(501, 0, 0, 500)])

    async def run_copy(self, mapping=None):
        return await self.copier.copy(self.snapshot, self.destination, mapping or {}, "test")

    async def test_complete_copy_order_membership_existing_channels_and_resume(self):
        report = await self.run_copy()
        self.assertIn("4/4", report)
        self.assertNotIn("MISMATCH", report)
        self.assertEqual(len(self.http.calls), 4)
        job = self.copier.job(self.snapshot, self.destination)
        payloads = {int(c["id"]): c for c in self.http.channels}
        category = job["created"]["2"]
        self.assertTrue(payloads[category]["nsfw"])
        self.assertEqual(payloads[job["created"]["3"]]["parent_id"], category)
        self.assertIsNone(payloads[job["created"]["1"]]["parent_id"])
        self.assertEqual(payloads[501]["parent_id"], "500")
        self.assertTrue(all(change["id"] not in (500, 501)
                            for change in self.http.position_payloads[-1]))
        await self.run_copy()
        self.assertEqual(len(self.http.calls), 4, "resuming must not duplicate channels")

    async def test_all_supported_creation_types_use_actual_library_serialization(self):
        channels = [raw(1, 4)]
        for index, kind in enumerate((0, 2, 5, 13, 15, 16), start=2):
            channels.append(raw(index, kind, index, 1, nsfw=True, rate_limit_per_user=5,
                                topic="topic", bitrate=64000, user_limit=2,
                                available_tags=[{"id": "88", "name": "tag", "moderated": True,
                                                 "emoji_id": None, "emoji_name": "⭐"}]))
        source, _ = guild(channels=channels)
        self.snapshot = capture(source, source.channels)
        report = await self.run_copy()
        self.assertNotIn("FAILED", report)
        self.assertEqual({kind for kind, _ in self.http.calls}, {0, 2, 4, 5, 13, 15, 16})
        text = next(payload for kind, payload in self.http.calls if kind == 0)
        self.assertEqual(text["topic"], "topic")
        self.assertEqual(text["rate_limit_per_user"], 5)
        forum = next(payload for kind, payload in self.http.calls if kind == 15)
        self.assertEqual(forum["available_tags"][0]["name"], "tag")
        self.assertNotIn("default_reaction_emoji", forum)
        self.assertEqual(len(self.http.channels), 9)
        media = next(payload for kind, payload in self.http.calls if kind == 16)
        self.assertNotIn("default_forum_layout", media)

    async def test_forbidden_partial_failure_resumes_only_missing_channels(self):
        self.http.failure = http_error()
        self.http.fail_after = 2
        report = await self.run_copy()
        self.assertIn("HTTP 403", report)
        self.assertNotIn("deliberately hidden details", report)
        self.assertEqual(len(self.http.calls), 2)
        self.assertIsNone(self.copier.job(self.snapshot, self.destination)["pending"])
        await self.run_copy()
        self.assertEqual(len(self.http.calls), 4)

    async def test_timeout_blocks_blind_retry(self):
        self.http.failure = TimeoutError()
        report = await self.run_copy()
        self.assertIn("outcome uncertain", report)
        with self.assertRaisesRegex(ValueError, "uncertain"):
            await self.run_copy()
        self.assertEqual(self.http.calls, [])

    async def test_server_error_blocks_blind_retry(self):
        self.http.failure = http_error(502)
        await self.run_copy()
        self.assertIsNotNone(self.copier.job(self.snapshot, self.destination)["pending"])

    async def test_rate_limit_is_reported_and_resumable(self):
        self.http.failure = discord.RateLimited(60)
        report = await self.run_copy()
        self.assertIn("rate limit", report)
        await self.run_copy()
        self.assertEqual(len(self.http.calls), 4)

    async def test_positions_failure_retries_without_duplicates(self):
        self.http.position_failure = http_error()
        report = await self.run_copy()
        self.assertIn("POSITION/VERIFY FAILED", report)
        report = await self.run_copy()
        self.assertNotIn("FAILED", report)
        self.assertEqual(len(self.http.calls), 4)

    async def test_settings_failure_retries_without_duplicates(self):
        self.http.edit_failure = http_error()
        report = await self.run_copy()
        self.assertIn("SETTING FAILED", report)
        report = await self.run_copy()
        self.assertNotIn("FAILED", report)
        self.assertEqual(len(self.http.calls), 4)

    async def test_missing_previously_created_channel_stops(self):
        await self.run_copy()
        self.http.channels.pop()
        with self.assertRaisesRegex(ValueError, "missing"):
            await self.run_copy()
        self.assertEqual(len(self.http.calls), 4)

    async def test_category_blocking_propagates_without_uncategorizing_children(self):
        category = next(c for c in self.snapshot["channels"] if c["type"] == 4)
        category["member_overwrites_omitted"] = True
        report = await self.run_copy()
        self.assertIn("Parent category is blocked", report)
        self.assertEqual(len(self.http.calls), 1)

    async def test_unavailable_parent_never_becomes_uncategorized(self):
        self.snapshot["channels"] = [c for c in self.snapshot["channels"] if c["type"] != 4]
        report = await self.run_copy()
        self.assertIn("parent category was inaccessible", report)
        self.assertEqual(len(self.http.calls), 1)

    async def test_snapshot_mutation_is_rejected_on_resume(self):
        await self.run_copy()
        self.snapshot["channels"][0]["name"] = "changed"
        with self.assertRaisesRegex(ValueError, "Snapshot changed"):
            await self.run_copy()

    async def test_concurrent_copy_is_rejected(self):
        import asyncio
        lock = self.copier.locks.setdefault(self.destination.id, asyncio.Lock())
        async with lock:
            with self.assertRaisesRegex(ValueError, "running"):
                await self.run_copy()

    async def test_unsupported_types_are_reported(self):
        self.snapshot["channels"][0]["type"] = 999
        report = await self.run_copy()
        self.assertIn("Unsupported channel type 999", report)
        self.assertNotIn(999, [kind for kind, _ in self.http.calls])

    async def test_community_feature_gate(self):
        source, _ = guild(channels=[raw(1, 15)])
        self.snapshot = capture(source, source.channels)
        self.destination.features = []
        report = await self.run_copy()
        self.assertIn("requires Community", report)

    async def test_missing_bot_permissions_prevent_all_creation(self):
        destination, http = guild(200, roles=[role(200, 1024)])
        with self.assertRaisesRegex(ValueError, "Manage Channels"):
            await self.copier.copy(self.snapshot, destination, {}, "test")
        self.assertEqual(http.calls, [])

    async def test_saved_role_mappings_cannot_be_changed(self):
        job = self.copier.job(self.snapshot, self.destination)
        job["role_map"] = {"110": "210"}
        self.store.write("jobs", self.copier.key(self.snapshot, self.destination), job)
        with self.assertRaisesRegex(ValueError, "cannot be changed"):
            await self.run_copy({"110": "220"})

    async def test_snapshot_content_and_permission_overwrites_round_trip(self):
        source, _ = guild(channels=[raw(1, permission_overwrites=[
            {"id": "100", "type": 0, "allow": "0", "deny": "1024"},
            {"id": "110", "type": 0, "allow": "1024", "deny": "2048"}])],
            roles=[role(100, 8), role(110)])
        destination, http = guild(200, roles=[role(200, 8), role(210)])
        snapshot = capture(source, source.channels)
        self.store.write("snapshots", snapshot["id"], snapshot)
        snapshot = self.store.read("snapshots", snapshot["id"])
        await self.copier.copy(snapshot, destination, {"110": "210"}, "test")
        overwrites = http.channels[0]["permission_overwrites"]
        self.assertEqual({o["id"] for o in overwrites}, {200, 210})
        self.assertEqual(next(o for o in overwrites if o["id"] == 210)["deny"], 2048)


class StorageTests(unittest.TestCase):
    def test_round_trip_delete_and_path_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            store.write("snapshots", "abc", {"id": "abc", "name": "⭐"})
            self.assertEqual(store.read("snapshots", "abc")["name"], "⭐")
            self.assertEqual(len(list(store.list_snapshots())), 1)
            with self.assertRaises(ValueError):
                store.read("snapshots", "../../.env")
            store.delete("abc")
            self.assertEqual(list(store.list_snapshots()), [])

    def test_corrupt_listing_does_not_hide_other_snapshots(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            store.write("snapshots", "good", {"id": "good"})
            store.path("snapshots", "bad").write_text("{", encoding="utf-8")
            self.assertEqual(list(store.list_snapshots()), [{"id": "good"}])

    def test_process_lock_prevents_second_writer(self):
        with tempfile.TemporaryDirectory() as folder:
            first = acquire_process_lock(Path(folder))
            try:
                with self.assertRaises(RuntimeError):
                    acquire_process_lock(Path(folder))
            finally:
                first.close()
            acquire_process_lock(Path(folder)).close()


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_role_permissions_override_stale_admin_role(self):
        source, _ = guild(roles=[role(100, 8)])
        member = source.me
        fresh_roles = [discord.Role(guild=source, state=source._state, data=role(100, 0))]
        permissions = main.effective_guild_permissions(100, 42, member, fresh_roles)
        self.assertFalse(permissions.administrator)

    async def test_arbitrary_destination_id_does_not_authorize_user(self):
        with patch.object(main.bot, "get_guild", return_value=None):
            with self.assertRaisesRegex(ValueError, "not connected"):
                await main.require_admin(77, 123456789)

    async def test_require_admin_rejects_unverified_membership(self):
        target = SimpleNamespace(fetch_member=AsyncMock(side_effect=discord.NotFound(
            SimpleNamespace(status=404, reason="missing"), "missing")))
        with patch.object(main.bot, "get_guild", return_value=target):
            with self.assertRaisesRegex(ValueError, "verify"):
                await main.require_admin(77, 200)

    async def test_require_admin_checks_actual_destination_membership(self):
        real, _ = guild(200, roles=[role(200, 0)])
        target = SimpleNamespace(id=200, fetch_member=AsyncMock(return_value=real.me),
                                 fetch_roles=AsyncMock(return_value=real.roles))
        with patch.object(main.bot, "get_guild", return_value=target), \
             patch.object(main.bot, "fetch_guild", AsyncMock(return_value=SimpleNamespace(owner_id=42))):
            with self.assertRaisesRegex(ValueError, "Administrator"):
                await main.require_admin(9, 200)

    async def test_confirmation_is_bound_to_requester_and_single_use(self):
        action = AsyncMock()
        view = main.Confirmation(7, action)
        click = SimpleNamespace(user=SimpleNamespace(id=8))
        with patch.object(main, "reply", AsyncMock()):
            self.assertFalse(await view.interaction_check(click))
        click.user.id = 7
        click.response = SimpleNamespace(edit_message=AsyncMock())
        self.assertTrue(await view.interaction_check(click))
        await view.confirm.callback(click)
        with patch.object(main, "reply", AsyncMock()):
            await view.confirm.callback(click)
        action.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
