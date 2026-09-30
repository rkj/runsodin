#!/usr/bin/env python3
"""Focused offline regressions for the Moonraker telemetry fixes.

Run inside the ODIN image: python3 tests/homelab/test_moonraker.py [source-root]
The test creates a temporary SQLite DB and never connects to a printer.
"""

import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


class MoonrakerFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(prefix="odin-moonraker-test-")
        root = Path(sys.argv[1] if len(sys.argv) > 1 else "/app")
        sys.path.insert(0, str(root / "backend"))
        os.environ["DATABASE_URL"] = f"sqlite:///{cls.scratch.name}/test.db"

        from sqlalchemy import event, text
        from sqlalchemy.orm import Session
        from core.db import engine
        from core.db_compat import execute_insert_returning_id
        from core.schema.bootstrap import bootstrap_database
        from core.ws_hub import push_event
        from modules.printers.adapters.moonraker import (
            MoonrakerPrinter, MoonrakerState, MoonrakerStatus,
        )
        import modules.printers.monitors.moonraker_monitor as monitor_module

        cls.text = staticmethod(text)
        cls.Session = Session
        cls.execute_insert_returning_id = staticmethod(execute_insert_returning_id)
        cls.engine = engine
        cls.push_event = staticmethod(push_event)
        cls.MoonrakerPrinter = MoonrakerPrinter
        cls.MoonrakerState = MoonrakerState
        cls.MoonrakerStatus = MoonrakerStatus
        cls.monitor_module = monitor_module

        # Keep the original failure fast while preserving the real SQLite
        # two-writer behavior. Connection pool is otherwise idle here.
        @event.listens_for(engine, "connect")
        def short_busy_timeout(dbapi_connection, _record):
            dbapi_connection.execute("PRAGMA busy_timeout=150")

        engine.dispose()
        bootstrap_database(engine, root / "backend")
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO printers (id, name) VALUES (42, 'test')"))
            conn.execute(text(
                "CREATE TABLE insert_probe (id INTEGER PRIMARY KEY, value TEXT)"
            ))

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        cls.scratch.cleanup()

    def setUp(self):
        with self.engine.begin() as conn:
            for table in ("print_jobs", "jobs", "print_files", "models", "education_submissions"):
                conn.execute(self.text(f"DELETE FROM {table}"))

    def test_object_query_encodes_sensor_names(self):
        printer = self.MoonrakerPrinter("printer.invalid")
        printer._has_fan = False
        printer._temperature_sensors = ["temperature_sensor chamber & dryer"]
        printer._filament_sensors = ["filament_switch_sensor Tool #1"]
        printer._env_sensors = []
        paths = []

        def fake_get(path):
            paths.append(path)
            return {"result": {"status": {"print_stats": {"state": "standby"}}}}

        printer._get = fake_get
        result = printer.get_status()
        self.assertEqual(result.state, self.MoonrakerState.READY)
        self.assertEqual(len(paths), 1)
        self.assertIn("temperature_sensor%20chamber%20%26%20dryer", paths[0])
        self.assertIn("filament_switch_sensor%20Tool%20%231", paths[0])
        self.assertNotIn(" ", paths[0])

    def test_heartbeat_commits_before_ws_event_write(self):
        monitor = self.monitor_module.MoonrakerMonitor(42, "test", "printer.invalid")
        monitor._prev_state = "IDLE"  # isolate the heartbeat path
        status = self.MoonrakerStatus(
            state=self.MoonrakerState.READY,
            internal_state="IDLE",
            raw_data={
                "heater_bed": {"temperature": 29.5, "target": 0},
                "extruder": {"temperature": 24.5, "target": 0},
                "print_stats": {"state": "standby"},
            },
        )
        # This method imports the dispatcher but does not call it for IDLE.
        fake_dispatcher = types.ModuleType("modules.notifications.event_dispatcher")
        with mock.patch.dict(sys.modules, {fake_dispatcher.__name__: fake_dispatcher}), \
             mock.patch.object(self.monitor_module, "mqtt_republish", None), \
             mock.patch.object(self.monitor_module, "ws_push", self.push_event):
            monitor._process_status(status)

        with self.engine.connect() as conn:
            printer = conn.execute(self.text(
                "SELECT bed_temp, gcode_state FROM printers WHERE id=42"
            )).one()
            events = conn.execute(self.text(
                "SELECT event_type, data FROM ws_events ORDER BY id"
            )).all()
        self.assertEqual(printer[0], 29.5)
        self.assertEqual(printer[1], "STANDBY")
        self.assertEqual(len(events), 1, "WebSocket event was lost to a SQLite writer lock")
        self.assertEqual(events[0][0], "printer_telemetry")
        self.assertIn('"printer_id": 42', events[0][1])

    def test_insert_helper_accepts_connection_and_session(self):
        statement = "INSERT INTO insert_probe (value) VALUES (:value)"
        with self.engine.begin() as conn:
            connection_id = self.execute_insert_returning_id(
                conn, statement, {"value": "connection"}
            )
        with self.Session(self.engine) as session:
            session_id = self.execute_insert_returning_id(
                session, statement, {"value": "session"}
            )
            session.commit()
        with self.engine.connect() as conn:
            rows = conn.execute(self.text(
                "SELECT id, value FROM insert_probe ORDER BY id"
            )).all()
        self.assertEqual(rows, [(connection_id, "connection"), (session_id, "session")])

    def test_auto_link_prefers_unlinked_dispatched_job_over_queued_copy(self):
        with self.engine.begin() as conn:
            conn.execute(self.text("INSERT INTO models (id, name) VALUES (1, 'same.gcode')"))
            conn.execute(self.text(
                "INSERT INTO print_files (id, model_id, filename, original_filename, layer_count) "
                "VALUES (1, 1, 'same.gcode', 'same.gcode', 12)"
            ))
            conn.execute(self.text(
                "INSERT INTO jobs (id, model_id, item_name, printer_id, status) "
                "VALUES (1, 1, 'queued', 42, 'scheduled'), "
                "(2, 1, 'dispatched', 42, 'printing')"
            ))
            conn.execute(self.text(
                "INSERT INTO print_jobs (id, printer_id, started_at) "
                "VALUES (101, 42, datetime('now'))"
            ))
        monitor = self.monitor_module.MoonrakerMonitor(42, "test", "printer.invalid")
        monitor._current_job_db_id = 101
        monitor._try_auto_link("same.gcode", 12)
        with self.engine.connect() as conn:
            linked = conn.execute(self.text(
                "SELECT scheduled_job_id FROM print_jobs WHERE id=101"
            )).scalar_one()
        self.assertEqual(linked, 2)

    def test_auto_link_ignores_printing_job_already_linked_to_other_print(self):
        with self.engine.begin() as conn:
            conn.execute(self.text("INSERT INTO models (id, name) VALUES (1, 'same.gcode')"))
            conn.execute(self.text(
                "INSERT INTO print_files (id, model_id, filename, original_filename, layer_count) "
                "VALUES (1, 1, 'same.gcode', 'same.gcode', 12)"
            ))
            conn.execute(self.text(
                "INSERT INTO jobs (id, model_id, item_name, printer_id, status) "
                "VALUES (1, 1, 'linked', 42, 'printing'), "
                "(2, 1, 'queued', 42, 'scheduled')"
            ))
            conn.execute(self.text(
                "INSERT INTO print_jobs (id, printer_id, started_at, scheduled_job_id) "
                "VALUES (100, 42, datetime('now'), 1), "
                "(101, 42, datetime('now'), NULL)"
            ))
        monitor = self.monitor_module.MoonrakerMonitor(42, "test", "printer.invalid")
        monitor._current_job_db_id = 101
        monitor._try_auto_link("same.gcode", 12)
        with self.engine.connect() as conn:
            linked = conn.execute(self.text(
                "SELECT scheduled_job_id FROM print_jobs WHERE id=101"
            )).scalar_one()
        self.assertEqual(linked, 2)

    def test_dispatched_job_observation_links_and_completes_on_bootstrap_schema(self):
        with self.engine.begin() as conn:
            conn.execute(self.text("INSERT INTO models (id, name) VALUES (10, 'queue.gcode')"))
            conn.execute(self.text(
                "INSERT INTO print_files (id, model_id, filename, original_filename, layer_count) "
                "VALUES (10, 10, 'queue.gcode', 'queue.gcode', 12)"
            ))
            conn.execute(self.text(
                "INSERT INTO jobs (id, model_id, item_name, printer_id, status) "
                "VALUES (10, 10, 'queue test', 42, 'printing')"
            ))
        monitor = self.monitor_module.MoonrakerMonitor(42, "test", "printer.invalid")
        status = self.MoonrakerStatus(
            state=self.MoonrakerState.PRINTING,
            internal_state="RUNNING",
            filename="queue.gcode",
            total_layers=12,
            bed_target=60,
            nozzle_target=210,
            raw_print_state="printing",
        )
        from core.registry import registry

        # Neutralize notifications and archiving; keep all lifecycle SQL real.
        fake_dispatcher = types.ModuleType("modules.notifications.event_dispatcher")
        fake_dispatcher.increment_care_counters = mock.Mock()
        fake_dispatcher.dispatch_alert = mock.Mock()
        with mock.patch.object(registry, "get_provider", return_value=None), \
             mock.patch.dict(sys.modules, {fake_dispatcher.__name__: fake_dispatcher}), \
             mock.patch("modules.archives.archive.create_print_archive"), \
             mock.patch.dict(os.environ, {"MOONRAKER_FILAMENT_DEDUCTION": "false"}), \
             mock.patch.object(monitor, "_auto_deduct_filament", side_effect=AssertionError(
                 "local filament deduction must remain disabled")):
            monitor._job_started(status)
            self.assertIsNotNone(monitor._current_job_db_id, "observation insert failed")
            print_job_id = monitor._current_job_db_id
            with self.engine.connect() as conn:
                linked = conn.execute(self.text(
                    "SELECT scheduled_job_id FROM print_jobs WHERE id=:id"
                ), {"id": print_job_id}).scalar_one()
            self.assertEqual(linked, 10, "dispatched queue job was not linked")
            suppressed = monitor._job_ended(
                "completed", status, education_terminal_status="completed"
            )
        self.assertFalse(suppressed)
        self.assertIsNone(monitor._current_job_db_id)
        with self.engine.connect() as conn:
            print_job = conn.execute(self.text(
                "SELECT status, ended_at, scheduled_job_id FROM print_jobs WHERE id=:id"
            ), {"id": print_job_id}).one()
            queue_job = conn.execute(self.text(
                "SELECT status, actual_end FROM jobs WHERE id=10"
            )).one()
        self.assertEqual(print_job[0], "completed")
        self.assertIsNotNone(print_job[1])
        self.assertEqual(print_job[2], 10)
        self.assertEqual(queue_job[0], "completed")
        self.assertIsNotNone(queue_job[1])

    def test_completion_dispatch_uses_current_module_path(self):
        monitor = self.monitor_module.MoonrakerMonitor(42, "test", "printer.invalid")
        with mock.patch.object(self.monitor_module.time, "sleep"), \
             mock.patch("modules.printers.dispatch.attempt_dispatch") as attempt:
            monitor._try_dispatch()
        attempt.assert_called_once_with(42)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
