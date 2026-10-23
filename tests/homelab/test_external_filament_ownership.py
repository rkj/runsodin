"""Offline checks for the maintained deployment's external accounting boundary."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("display_fixture", Path(__file__).parents[1] / "printers/test_filament_ledger_display.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
from modules.printers.monitors.moonraker_monitor import MoonrakerMonitor

class ExternalFilamentOwnership(unittest.TestCase):
    def test_disabled_deduction_never_queries_or_records_inventory(self):
        monitor = MoonrakerMonitor.__new__(MoonrakerMonitor)
        connection = Mock()
        with patch.dict(os.environ, {"MOONRAKER_FILAMENT_DEDUCTION": "false"}):
            monitor._auto_deduct_filament(connection, 123, observed_grams=5)
        connection.execute.assert_not_called()

    def test_enabled_deduction_keeps_existing_job_lookup(self):
        monitor = MoonrakerMonitor.__new__(MoonrakerMonitor)
        monitor.printer_id = 7
        connection = Mock()
        connection.execute.return_value.first.return_value = None
        with patch.dict(os.environ, {"MOONRAKER_FILAMENT_DEDUCTION": "true"}):
            monitor._auto_deduct_filament(connection, 123, observed_grams=5)
        connection.execute.assert_called_once()

    def test_mmu_sync_normalizes_materials_before_fresh_serialization(self):
        fixture.FilamentLedgerDisplay.setUpClass()
        case = fixture.FilamentLedgerDisplay()
        case.setUp()
        try:
            monitor = MoonrakerMonitor.__new__(MoonrakerMonitor)
            monitor.printer_id = 7
            monitor.name = "Synthetic MMU"
            for material, expected in (("PLA+", "PLA"), ("custom blend", "OTHER"), ("", "empty")):
                monitor._sync_filament_slots([SimpleNamespace(gate=0, material=material, color_hex="3366AA", name="Synthetic")])
                with fixture.SessionLocal() as db:
                    from modules.printers.models import Printer
                    from modules.printers.schemas import PrinterResponse
                    response = PrinterResponse.model_validate(db.get(Printer, 7))
                    self.assertEqual(response.filament_slots[0].filament_type.value, expected)
        finally:
            case.doCleanups()

if __name__ == "__main__":
    unittest.main(verbosity=2)
