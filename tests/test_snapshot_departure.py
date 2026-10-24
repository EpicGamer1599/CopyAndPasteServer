"""Successful scans must save before leaving; failed scans must stay retryable."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

import main
from storage import Store
from structure import Copier
from test_bot import guild, http_error, raw


class SnapshotDepartureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.copier = Copier(self.store)
        self.source, _ = guild(channels=[raw(1)])
        self.events = []

        async def leave():
            self.events.append("leave")
            self.assertEqual(len(list(self.store.list_snapshots())), 1)

        async def notify(*args, **kwargs):
            self.events.append("notify")
            self.assertEqual(len(list(self.store.list_snapshots())), 1)
            if "file" in kwargs:
                kwargs["file"].close()

        self.target = SimpleNamespace(
            id=self.source.id,
            fetch_channels=AsyncMock(return_value=self.source.channels),
            leave=AsyncMock(side_effect=leave),
        )
        self.interaction = SimpleNamespace(
            guild_id=self.source.id, user=SimpleNamespace(id=42),
            response=SimpleNamespace(defer=AsyncMock()),
        )
        self.notify = AsyncMock(side_effect=notify)
        patches = [
            patch.object(main, "store", self.store),
            patch.object(main, "copier", self.copier),
            patch.object(main, "require_admin", AsyncMock(return_value=self.target)),
            patch.object(main, "require_bot", AsyncMock(return_value=self.target)),
            patch.object(main, "capture", side_effect=lambda *args: main_capture(self.source)),
            patch.object(main, "reply", self.notify),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    async def run_snapshot(self):
        await main.snapshot_command.callback(self.interaction)

    async def test_snapshot_is_persisted_and_announced_before_leaving(self):
        await self.run_snapshot()
        self.assertEqual(self.events, ["notify", "leave"])
        self.target.leave.assert_awaited_once()
        self.assertIn("leaving this source server", self.notify.call_args.args[1])
        self.assertIn("Re-invite", self.notify.call_args.args[1])

    async def test_fetch_failure_does_not_leave_or_save(self):
        self.target.fetch_channels.side_effect = http_error()
        with self.assertRaises(discord.HTTPException):
            await self.run_snapshot()
        self.target.leave.assert_not_awaited()
        self.notify.assert_not_awaited()
        self.assertEqual(list(self.store.list_snapshots()), [])

    async def test_save_failure_does_not_leave(self):
        with patch.object(self.store, "write", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                await self.run_snapshot()
        self.target.leave.assert_not_awaited()
        self.notify.assert_not_awaited()

    async def test_notification_failure_still_leaves_after_saving(self):
        async def failed_notify(*args, **kwargs):
            if "file" in kwargs:
                kwargs["file"].close()
            raise http_error()

        self.notify.side_effect = failed_notify
        await self.run_snapshot()
        self.target.leave.assert_awaited_once()
        self.assertEqual(len(list(self.store.list_snapshots())), 1)

    async def test_leave_failure_reports_saved_snapshot_and_manual_removal(self):
        self.target.leave.side_effect = http_error()
        await self.run_snapshot()
        self.assertEqual(self.notify.await_count, 2)
        message = self.notify.call_args.args[1]
        self.assertIn("safely saved", message)
        self.assertIn("remove me manually", message)
        self.assertEqual(len(list(self.store.list_snapshots())), 1)

    async def test_unauthorized_invocation_does_not_scan_or_leave(self):
        with patch.object(main, "require_admin", AsyncMock(side_effect=ValueError("Administrator required"))):
            with self.assertRaises(ValueError):
                await self.run_snapshot()
        self.target.fetch_channels.assert_not_awaited()
        self.target.leave.assert_not_awaited()


    async def test_active_destination_copy_blocks_snapshot_before_scan_or_departure(self):
        lock = self.copier.locks.setdefault(self.target.id, asyncio.Lock())
        async with lock:
            with self.assertRaisesRegex(ValueError, "running"):
                await self.run_snapshot()
        self.target.fetch_channels.assert_not_awaited()
        self.target.leave.assert_not_awaited()
        self.notify.assert_not_awaited()
        self.assertEqual(list(self.store.list_snapshots()), [])

    async def test_departure_blocks_concurrent_copy_and_duplicate_snapshot(self):
        notifying = asyncio.Event()
        release = asyncio.Event()

        async def delayed_notify(*args, **kwargs):
            notifying.set()
            await release.wait()

        self.notify.side_effect = delayed_notify
        task = asyncio.create_task(self.run_snapshot())
        try:
            await asyncio.wait_for(notifying.wait(), timeout=2)
            with self.assertRaisesRegex(ValueError, "running"):
                await self.copier.copy(main_capture(self.source), self.source, {}, "test")
            with self.assertRaisesRegex(ValueError, "running"):
                await self.run_snapshot()
            self.target.fetch_channels.assert_awaited_once()
            self.assertEqual(len(list(self.store.list_snapshots())), 1)
        finally:
            release.set()
            await task
        self.target.leave.assert_awaited_once()
        self.assertFalse(self.copier.locks[self.target.id].locked())

    async def test_notification_and_departure_failure_do_not_mask_saved_snapshot(self):
        self.notify.side_effect = http_error()
        self.target.leave.side_effect = http_error()
        with self.assertLogs(main.__name__, level="WARNING") as logs:
            await self.run_snapshot()
        self.assertEqual(self.notify.await_count, 2)
        self.target.leave.assert_awaited_once()
        self.assertEqual(len(list(self.store.list_snapshots())), 1)
        self.assertTrue(any("departure warning could not be delivered" in message for message in logs.output))
        self.assertTrue(any("notification failed" in message for message in logs.output))
        self.assertFalse(self.copier.locks[self.target.id].locked())

    async def test_attachment_closed_when_notification_fails_before_send(self):
        attachment = main.text_file("saved", "preview.txt")
        self.notify.side_effect = http_error()
        with patch.object(main, "text_file", return_value=attachment):
            await self.run_snapshot()
        self.assertTrue(attachment.fp.closed)
        self.target.leave.assert_awaited_once()

    async def test_scan_failure_releases_guild_operation_lock(self):
        self.target.fetch_channels.side_effect = http_error()
        with self.assertRaises(discord.HTTPException):
            await self.run_snapshot()
        self.assertFalse(self.copier.locks[self.target.id].locked())
        self.target.fetch_channels.side_effect = None
        await self.run_snapshot()
        self.target.leave.assert_awaited_once()

    async def test_network_notification_error_still_saves_and_leaves(self):
        self.notify.side_effect = OSError("connection closed")
        await self.run_snapshot()
        self.target.leave.assert_awaited_once()
        self.assertEqual(len(list(self.store.list_snapshots())), 1)


def main_capture(source):
    from structure import capture
    return capture(source, source.channels)
