from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
import discord
from discord.http import HTTPClient, Route

import main
from safe_http import AmbiguousCreate, create_trace, guarded_create
from storage import Store
from structure import Copier, capture, validate_snapshot
from test_bot import guild, raw, role


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, statuses):
        calls = []

        async def handler(request):
            status = statuses[min(len(calls), len(statuses) - 1)]
            calls.append(status)
            if status == -1:
                request.transport.close()
                return web.Response()
            if status == 429:
                return web.Response(body=json.dumps({"retry_after": 0.001, "global": False}).encode(),
                                    status=429, headers={"Via": "discord", "Content-Type": "application/json"})
            return web.Response(body=b'{"id": "1001", "code": 0}', status=status,
                                headers={"Content-Type": "application/json"})

        app = web.Application()
        app.router.add_post("/guilds/200/channels", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        http = HTTPClient(asyncio.get_running_loop())
        http._global_over = asyncio.Event()
        http._global_over.set()
        route = Route("POST", "/guilds/{guild_id}/channels", guild_id=200)
        route.url = f"http://127.0.0.1:{port}/guilds/200/channels"
        async with aiohttp.ClientSession(trace_configs=[create_trace()]) as session:
            http._HTTPClient__session = session
            try:
                with guarded_create():
                    result = await http.request(route, json={"name": "test"})
                return calls, result
            except (AmbiguousCreate, aiohttp.ClientError, OSError):
                return calls, "ambiguous"

    async def test_real_http_client_never_resends_after_uncertain_server_error(self):
        calls, result = await self.request([502, 200])
        self.assertEqual(calls, [502])
        self.assertEqual(result, "ambiguous")

    async def test_rate_limit_rejection_can_retry(self):
        calls, result = await self.request([429, 200])
        self.assertEqual(calls, [429, 200])
        self.assertEqual(result["id"], "1001")

    async def test_rate_limit_then_server_error_cannot_send_third_request(self):
        calls, result = await self.request([429, 500, 200])
        self.assertEqual(calls, [429, 500])
        self.assertEqual(result, "ambiguous")

    async def test_connection_reset_does_not_resend_create(self):
        calls, result = await self.request([-1, 200])
        self.assertEqual(calls, [-1])
        self.assertEqual(result, "ambiguous")

    async def test_guard_is_reset_between_operations(self):
        trace = create_trace()
        params = SimpleNamespace(method="POST")
        for _ in range(2):
            with guarded_create():
                await trace.on_request_start[0](None, None, params)
                with self.assertRaises(AmbiguousCreate):
                    await trace.on_request_start[0](None, None, params)


class AuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.copier = Copier(self.store)
        self.source, _ = guild(channels=[raw(1, 4), raw(2, 0, 0, 1)])
        self.snapshot = capture(self.source, self.source.channels)
        self.destination, self.http = guild(200)

    async def test_moved_parent_stops_resume_before_any_discord_write(self):
        await self.copier.copy(self.snapshot, self.destination, {}, "test")
        job = self.copier.job(self.snapshot, self.destination)
        child = next(c for c in self.http.channels if int(c["id"]) == job["created"]["2"])
        child["parent_id"] = None
        writes = len(self.http.position_payloads)
        with self.assertRaisesRegex(ValueError, "parent changed"):
            await self.copier.copy(self.snapshot, self.destination, {}, "test")
        self.assertEqual(len(self.http.calls), 2)
        self.assertEqual(len(self.http.position_payloads), writes)

    async def test_retyped_category_cannot_be_reused_as_parent(self):
        await self.copier.copy(self.snapshot, self.destination, {}, "test")
        self.http.channels[0]["type"] = 0
        with self.assertRaisesRegex(ValueError, "type or parent"):
            await self.copier.copy(self.snapshot, self.destination, {}, "test")

    async def test_foreign_journal_is_rejected(self):
        job = self.copier.job(self.snapshot, self.destination)
        job["destination_id"] = "999"
        self.store.write("jobs", self.copier.key(self.snapshot, self.destination), job)
        with self.assertRaisesRegex(ValueError, "journal"):
            await self.copier.copy(self.snapshot, self.destination, {}, "test")
        self.assertFalse(self.http.calls)

    async def test_changed_role_definitions_prevent_copy_despite_stale_cache(self):
        self.http.roles = [role(200, 0)]
        with self.assertRaisesRegex(ValueError, "Manage Channels"):
            await self.copier.copy(self.snapshot, self.destination, {}, "test")
        self.assertFalse(self.http.calls)

    async def test_new_admin_role_missing_from_cache_is_used_for_authorization(self):
        target, _ = guild(200, roles=[role(200, 0)])
        member = discord.Member(data={"user": {"id": "7", "username": "admin", "discriminator": "0", "avatar": None},
                                      "roles": ["210"], "flags": 0}, guild=target, state=target._state)
        roles = [*target.roles, discord.Role(guild=target, state=target._state, data=role(210, 8))]
        self.assertTrue(main.effective_guild_permissions(200, 42, member, roles).administrator)

    async def test_bot_check_returns_fresh_permission_context(self):
        stale, _ = guild(200, roles=[role(200, 0)])
        fresh, _ = guild(200)
        with patch.object(main.bot._connection, "user", fresh._state.user), \
             patch.object(main.bot, "fetch_guild", AsyncMock(return_value=fresh)), \
             patch.object(discord.Guild, "fetch_member", AsyncMock(return_value=fresh.me)):
            result = await main.require_bot(stale)
        self.assertIs(result, fresh)
        self.assertTrue(result.me.guild_permissions.administrator)

    async def test_cancel_after_confirm_does_not_claim_no_changes(self):
        action = AsyncMock()
        view = main.Confirmation(7, action)
        click = SimpleNamespace(user=SimpleNamespace(id=7), response=SimpleNamespace(edit_message=AsyncMock()))
        await view.confirm.callback(click)
        with patch.object(main, "reply", AsyncMock()) as reply:
            await view.cancel.callback(click)
            self.assertIn("already used", reply.call_args.args[1])
        self.assertEqual(click.response.edit_message.await_count, 1)
        action.assert_awaited_once()

    async def test_source_and_destination_authorization_are_independent(self):
        interaction = SimpleNamespace(user=SimpleNamespace(id=7), guild_id=100)
        with patch.object(main, "source_snapshot", AsyncMock(return_value=(self.snapshot, self.source))), \
             patch.object(main, "require_admin", AsyncMock(side_effect=ValueError("destination denied"))) as auth:
            with self.assertRaisesRegex(ValueError, "destination denied"):
                await main.copy_access(interaction, self.snapshot["id"], "200")
        auth.assert_awaited_once_with(7, 200)

    async def test_same_guild_is_rejected_before_bot_permission_check(self):
        interaction = SimpleNamespace(user=SimpleNamespace(id=7), guild_id=100)
        with patch.object(main, "source_snapshot", AsyncMock(return_value=(self.snapshot, self.source))), \
             patch.object(main, "require_admin", AsyncMock(return_value=self.source)):
            with self.assertRaisesRegex(ValueError, "separate"):
                await main.copy_access(interaction, self.snapshot["id"], "100")

    async def test_malformed_snapshots_are_skipped_by_listing(self):
        self.store.write("snapshots", "broken", {"id": "broken"})
        self.store.write("snapshots", self.snapshot["id"], self.snapshot)
        with patch.object(main, "store", self.store):
            self.assertEqual(list(main.saved_snapshots()), [self.snapshot])

    async def test_invalid_json_root_is_rejected(self):
        self.store.path("snapshots", "broken").write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "object"):
            self.store.read("snapshots", "broken")
        self.assertEqual(list(self.store.list_snapshots()), [])

    async def test_duplicate_source_ids_never_reach_discord(self):
        self.snapshot["channels"].append(copy.deepcopy(self.snapshot["channels"][0]))
        with self.assertRaisesRegex(ValueError, "Malformed snapshot"):
            await self.copier.copy(self.snapshot, self.destination, {}, "test")
        self.assertFalse(self.http.calls)

    async def test_bad_overwrite_metadata_is_rejected(self):
        self.snapshot["channels"][0]["overwrites"] = [
            {"role_id": "100", "everyone": False, "allow": 0, "deny": 1024}]
        with self.assertRaisesRegex(ValueError, "Malformed snapshot"):
            validate_snapshot(self.snapshot)


    async def test_complex_copy_preserves_exact_relative_ui_order(self):
        from structure import ordered_channels
        source, _ = guild(channels=[raw(50, 4, 3), raw(40, 4, 1), raw(90, 2, 0, 40),
                                   raw(72, 0, 2, 40), raw(71, 0, 2, 40), raw(80, 15, 0, 50),
                                   raw(82, 13, 2, 50), raw(11, 2, 0), raw(12, 0, 9), raw(60, 4, 4)])
        snapshot = capture(source, source.channels)
        destination, http = guild(200, channels=[raw(800, 4, 5), raw(801, 0, 7, 800), raw(802, 2, 3)])
        report = await self.copier.copy(snapshot, destination, {}, "test")
        self.assertNotIn("MISMATCH", report)
        job = self.copier.job(snapshot, destination)
        copied = set(job["created"].values())
        actual = [c.id for c in ordered_channels(await destination.fetch_channels()) if c.id in copied]
        self.assertEqual(actual, [job["created"][c["id"]] for c in snapshot["channels"]])
        for entry in snapshot["channels"]:
            channel = next(c for c in await destination.fetch_channels() if c.id == job["created"][entry["id"]])
            self.assertEqual(channel.category_id, job["created"].get(entry["parent_id"]))

    async def test_cancelled_create_retains_pending_and_prevents_retry(self):
        self.http.failure = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.copier.copy(self.snapshot, self.destination, {}, "test")
        job = self.copier.job(self.snapshot, self.destination)
        self.assertEqual(job["pending"], "1")
        self.assertTrue(any("INTERRUPTED" in line for line in job["report"]))
        with self.assertRaisesRegex(ValueError, "uncertain"):
            await self.copier.copy(self.snapshot, self.destination, {}, "test")
        self.assertFalse(self.http.calls)

    async def test_disk_replace_failure_keeps_previous_checkpoint(self):
        self.store.write("jobs", "test", {"pending": "1"})
        with patch("storage.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.store.write("jobs", "test", {"pending": None})
        self.assertEqual(self.store.read("jobs", "test"), {"pending": "1"})

    async def test_renamed_snapshot_file_is_not_listed_with_wrong_id(self):
        self.store.write("snapshots", "wrong", self.snapshot)
        self.assertEqual(list(self.store.list_snapshots()), [])

    async def test_snapshot_id_must_match_selected_file(self):
        self.store.write("snapshots", "wrong", self.snapshot)
        interaction = SimpleNamespace(user=SimpleNamespace(id=7), guild_id=100)
        with patch.object(main, "store", self.store), \
             patch.object(main, "require_admin", AsyncMock(return_value=self.source)):
            with self.assertRaisesRegex(ValueError, "belonging"):
                await main.source_snapshot(interaction, "wrong")
