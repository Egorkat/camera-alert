import datetime
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "/opt/camera-alert/tools")

import nas_clip_cleanup


class NasClipCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        nas_clip_cleanup.CLIP_DIR = str(root / "clips")
        nas_clip_cleanup.FTP_DIR = str(root / "ftp")
        nas_clip_cleanup.DB_PATH = str(root / "events.db")
        nas_clip_cleanup.STATE_DIR = root / "state"
        nas_clip_cleanup.PENDING_DIR = root / "state" / "pending"
        nas_clip_cleanup.DONE_DIR = root / "state" / "done"
        with sqlite3.connect(nas_clip_cleanup.DB_PATH):
            pass

    def tearDown(self):
        self.tmpdir.cleanup()

    def _yesterday(self):
        return (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

    def test_waits_full_grace_period_then_removes_all_matching_local_folders(self):
        day = self._yesterday()
        source_day = datetime.date.fromisoformat(day).strftime("%d-%m")
        clip_day = Path(nas_clip_cleanup.CLIP_DIR) / source_day
        clip_day.mkdir(parents=True)
        ftp_days = [
            Path(nas_clip_cleanup.FTP_DIR) / camera / day
            for camera in ("Dacha1", "Dacha2", "Dacha3", "Dacha4")
        ]
        for ftp_day in ftp_days:
            ftp_day.mkdir(parents=True)
            (ftp_day / "recording.dav").touch()

        now = time.time()
        with patch.object(nas_clip_cleanup, "_nas_day_present", return_value=False):
            nas_clip_cleanup._process_day(day, now=now)
            self.assertTrue(clip_day.exists())
            self.assertTrue(nas_clip_cleanup.PENDING_DIR.joinpath(f"{day}.pending").exists())

            pending = nas_clip_cleanup.PENDING_DIR / f"{day}.pending"
            os.utime(pending, (now - nas_clip_cleanup.CLEANUP_GRACE_SECONDS - 1,) * 2)
            with patch.object(nas_clip_cleanup, "_converter_is_active", return_value=False):
                nas_clip_cleanup._process_day(day, now=now)

        self.assertFalse(clip_day.exists())
        self.assertTrue(all(not ftp_day.exists() for ftp_day in ftp_days))
        self.assertTrue((nas_clip_cleanup.DONE_DIR / day).exists())
        with sqlite3.connect(nas_clip_cleanup.DB_PATH) as conn:
            status = conn.execute(
                "SELECT status FROM processed_days WHERE day=?", (day,)
            ).fetchone()[0]
        self.assertEqual(status, "deleted")

    def test_restoring_nas_folder_cancels_pending_cleanup(self):
        day = self._yesterday()
        nas_clip_cleanup._ensure_state_dirs()
        pending = nas_clip_cleanup.PENDING_DIR / f"{day}.pending"
        pending.touch()

        with patch.object(nas_clip_cleanup, "_nas_day_present", return_value=True):
            nas_clip_cleanup._process_day(day)

        self.assertFalse(pending.exists())

    def test_cleanup_stops_and_restarts_active_converter(self):
        day = self._yesterday()
        nas_clip_cleanup._ensure_state_dirs()
        pending = nas_clip_cleanup.PENDING_DIR / f"{day}.pending"
        pending.touch()
        old_time = time.time() - nas_clip_cleanup.CLEANUP_GRACE_SECONDS - 1
        os.utime(pending, (old_time, old_time))

        with (
            patch.object(nas_clip_cleanup, "_nas_day_present", return_value=False),
            patch.object(nas_clip_cleanup, "_converter_is_active", return_value=True),
            patch.object(nas_clip_cleanup.subprocess, "run") as systemctl,
        ):
            nas_clip_cleanup._process_day(day, now=time.time())

        self.assertEqual(
            [call.args[0] for call in systemctl.call_args_list],
            [
                ["systemctl", "stop", nas_clip_cleanup.CONVERTER_SERVICE],
                ["systemctl", "start", nas_clip_cleanup.CONVERTER_SERVICE],
            ],
        )

    def test_today_is_never_eligible_for_cleanup(self):
        day = datetime.date.today().isoformat()
        with patch.object(nas_clip_cleanup, "_nas_day_present") as check_nas:
            nas_clip_cleanup._process_day(day)
        check_nas.assert_not_called()


if __name__ == "__main__":
    unittest.main()
