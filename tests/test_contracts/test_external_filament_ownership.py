"""Maintained deployment can leave filament accounting to Filament Ledger."""
from unittest.mock import Mock

from modules.printers.monitors.moonraker_monitor import MoonrakerMonitor


def test_disabled_deduction_never_queries_or_records_inventory(monkeypatch):
    monkeypatch.setenv("MOONRAKER_FILAMENT_DEDUCTION", "false")
    monitor = MoonrakerMonitor.__new__(MoonrakerMonitor)
    connection = Mock()
    monitor._auto_deduct_filament(connection, 123, observed_grams=5)
    connection.execute.assert_not_called()


def test_enabled_deduction_keeps_existing_job_lookup(monkeypatch):
    monkeypatch.setenv("MOONRAKER_FILAMENT_DEDUCTION", "true")
    monitor = MoonrakerMonitor.__new__(MoonrakerMonitor)
    monitor.printer_id = 7
    connection = Mock()
    connection.execute.return_value.first.return_value = None
    monitor._auto_deduct_filament(connection, 123, observed_grams=5)
    connection.execute.assert_called_once()
