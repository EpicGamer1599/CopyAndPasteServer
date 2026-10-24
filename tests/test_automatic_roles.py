"""Real discord.py serialization: missing mappings must not skip the server."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import discord

import main
from storage import Store
from structure import Copier, capture, missing_role_ids, plan
from test_bot import guild, http_error, raw, role


def role_overwrite(role_id):
    return {"id": str(role_id), "type": 0, "allow": "1024", "deny": "2048"}


class AutomaticRoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        self.copier = Copier(self.store)
        self.source, _ = guild(channels=[raw(1, 4, permission_overwrites=[role_overwrite(110)]),
                                          raw(2, parent=1, permission_overwrites=[role_overwrite(110)])],
                               roles=[role(100, 8), {**role(110), "name": "Moderators"}])
        self.snapshot = capture(self.source, self.source.channels)
        self.destination, self.http = guild(200, roles=[role(200, 8), {**role(210), "name": "Moderators"}])

    async def copy(self, names=None, mapping=None):
        return await self.copier.copy(self.snapshot, self.destination, mapping or {}, "test",
                                      automatic_role_names=names if names is not None else {"110": "Moderators"})

    async def test_missing_role_is_created_with_new_id_and_full_channel_overwrites(self):
        report = await self.copy()
        self.assertIn("2/2", report)
        self.assertNotIn("BLOCKED", report)
        self.assertEqual(len(self.http.role_calls), 1)
        job = self.copier.job(self.snapshot, self.destination)
        destination_role_id = int(job["role_map"]["110"])
        self.assertNotIn(destination_role_id, (110, 210), "never assume a source ID or same-name role is a match")
        self.assertEqual(self.http.role_calls[0]["name"], "Moderators")
        self.assertEqual(int(self.http.role_calls[0]["permissions"]), 0)
        self.assertFalse(self.http.role_calls[0]["mentionable"])
        for channel in self.http.channels:
            overwrite = channel["permission_overwrites"][0]
            self.assertEqual(overwrite["id"], destination_role_id)
            self.assertEqual(overwrite["allow"], 1024)
            self.assertEqual(overwrite["deny"], 2048)

    async def test_resume_reuses_automatically_created_roles_and_channels(self):
        await self.copy()
        await self.copy()
        self.assertEqual(len(self.http.role_calls), 1)
        self.assertEqual(len(self.http.calls), 2)

    async def test_explicit_existing_role_mapping_is_still_supported(self):
        report = await self.copy(mapping={"110": "210"})
        self.assertNotIn("BLOCKED", report)
        self.assertEqual(self.http.role_calls, [])
        self.assertEqual(self.http.channels[0]["permission_overwrites"][0]["id"], 210)

    async def test_old_snapshot_without_role_names_does_not_require_rescanning(self):
        report = await self.copy(names={})
        self.assertNotIn("BLOCKED", report)
        self.assertEqual(self.http.role_calls[0]["name"], "Imported role 110")

    async def test_role_preview_has_no_writes_and_does_not_block_parent_or_children(self):
        job = self.copier.job(self.snapshot, self.destination)
        pending = missing_role_ids(self.snapshot, {})
        blocked = plan(self.snapshot, self.destination, self.destination.roles, {}, job, pending)
        self.assertEqual(blocked, {})
        self.assertEqual(self.http.role_calls, [])
        self.assertEqual(self.http.calls, [])

    async def test_role_permission_failure_preserves_existing_copy_progress(self):
        self.http.role_failure = http_error()
        report = await self.copy()
        self.assertIn("ROLE FAILED", report)
        job = self.copier.job(self.snapshot, self.destination)
        self.assertIsNone(job["pending_role"])
        self.assertEqual(self.http.calls, [])
        report = await self.copy()
        self.assertNotIn("FAILED", report)
        self.assertEqual(len(self.http.role_calls), 1)

    async def test_uncertain_role_create_does_not_blindly_duplicate_roles(self):
        self.http.role_failure = TimeoutError()
        report = await self.copy()
        self.assertIn("outcome uncertain", report)
        self.assertEqual(self.copier.job(self.snapshot, self.destination)["pending_role"], "110")
        with self.assertRaisesRegex(ValueError, "uncertain"):
            await self.copy()
        self.assertEqual(self.http.calls, [])

    async def test_roles_checkpointed_before_channel_failure_are_reused(self):
        self.http.failure = http_error()
        report = await self.copy()
        self.assertIn("FAILED", report)
        self.assertEqual(len(self.http.role_calls), 1)
        await self.copy()
        self.assertEqual(len(self.http.role_calls), 1)
        self.assertEqual(len(self.http.calls), 2)

    async def test_partial_role_failure_reuses_roles_from_the_first_attempt(self):
        self.snapshot["channels"][0]["overwrites"].append(
            {"role_id": "120", "everyone": False, "allow": 1024, "deny": 0})
        self.http.role_failure = http_error()
        self.http.role_fail_after = 1
        report = await self.copy(names={"110": "One", "120": "Two"})
        self.assertIn("ROLE FAILED", report)
        first_id = self.copier.job(self.snapshot, self.destination)["role_map"]["110"]
        await self.copy(names={"110": "One", "120": "Two"})
        self.assertEqual(len(self.http.role_calls), 2)
        self.assertEqual(self.copier.job(self.snapshot, self.destination)["role_map"]["110"], first_id)
        self.assertEqual(len(self.http.calls), 2)

    async def test_checkpoint_failure_after_role_creation_keeps_pending_marker(self):
        original_write = self.store.write
        def fail_after_role(folder, key, data):
            if data.get("role_map"):
                raise OSError("disk full")
            original_write(folder, key, data)
        with patch.object(self.store, "write", side_effect=fail_after_role):
            with self.assertRaises(OSError):
                await self.copy()
        self.assertEqual(len(self.http.role_calls), 1)
        self.assertEqual(self.copier.job(self.snapshot, self.destination)["pending_role"], "110")
        with self.assertRaisesRegex(ValueError, "uncertain"):
            await self.copy()
        self.assertEqual(len(self.http.role_calls), 1)

    async def test_resume_exact_fifteen_of_eighty_six_case(self):
        channels = [raw(index) for index in range(1, 16)]
        for index, role_id in ((16, 110), (17, 120), (18, 130)):
            channels.append(raw(index, 4, index, permission_overwrites=[role_overwrite(role_id)]))
        for index in range(19, 87):
            parent = 16 + index % 3
            channels.append(raw(index, parent=parent, position=index,
                                permission_overwrites=[role_overwrite(110 + (parent - 16) * 10)]))
        source, _ = guild(channels=channels, roles=[role(100, 8), role(110), role(120), role(130)])
        self.snapshot = capture(source, source.channels)
        report = await self.copier.copy(self.snapshot, self.destination, {}, "old-copy")
        self.assertIn("15/86", report)
        original_ids = dict(self.copier.job(self.snapshot, self.destination)["created"])
        report = await self.copy(names={"110": "Verified", "120": "Admins", "130": "Developers"})
        self.assertIn("86/86", report)
        self.assertNotIn("BLOCKED", report)
        self.assertNotIn("MISMATCH", report)
        self.assertEqual(len(self.http.calls), 86)
        self.assertEqual(len(self.http.role_calls), 3)
        current = self.copier.job(self.snapshot, self.destination)
        self.assertTrue(all(current["created"][key] == value for key, value in original_ids.items()))
        await self.copy(names={})
        self.assertEqual(len(self.http.calls), 86)
        self.assertEqual(len(self.http.role_calls), 3)

    async def test_application_copy_path_uses_automatic_roles_by_default(self):
        with patch.object(main, "copier", self.copier), \
             patch.object(main.bot, "get_guild", return_value=self.source), \
             patch.object(main, "reply", AsyncMock()):
            await main.run_copy(object(), self.snapshot, self.destination, {})
        self.assertEqual(len(self.http.role_calls), 1)
        self.assertEqual(self.http.role_calls[0]["name"], "Moderators")
        self.assertEqual(len(self.http.calls), 2)
