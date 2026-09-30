#!/usr/bin/env python3
"""Offline scan-assignment and printer-list regression on the bootstrap schema."""
import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest

BACKEND = Path(os.environ.get('ODIN_TEST_BACKEND', '/app/backend'))
scratch = tempfile.TemporaryDirectory(prefix='odin-filament-tests-')
sys.path.insert(0, str(BACKEND))
os.environ['DATABASE_URL'] = 'sqlite:///' + scratch.name + '/test.db'
os.environ['JWT_SECRET_KEY'] = 'synthetic-filament-test-secret-no-live-credentials'
from core.db import engine, SessionLocal
from core.schema.bootstrap import bootstrap_database
from sqlalchemy import text
for path in (BACKEND / 'modules').glob('*/models.py'):
    importlib.import_module('.'.join(path.relative_to(BACKEND).with_suffix('').parts))
from modules.inventory.routes.spool_ops import scan_assign_spool
from modules.inventory.routes._helpers import ScanAssignRequest
from modules.printers.routes_crud import list_printers
from modules.printers.schemas import PrinterResponse

class FilamentAssignments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bootstrap_database(engine, BACKEND)
    def setUp(self):
        with engine.begin() as c:
            for table in ('filament_slots', 'spools', 'filament_library', 'printers'):
                c.execute(text('DELETE FROM ' + table))
            c.execute(text("INSERT INTO printers(id,name,slot_count,is_active,tags,timelapse_enabled,shared) VALUES(42,'Synthetic printer',1,1,'[]',0,0)"))
    def assert_readable(self, expected):
        with SessionLocal() as db:
            rows = list_printers(False, None, None, {'role':'admin'}, {}, db)
            result = PrinterResponse.model_validate(rows[0])
            self.assertEqual(result.filament_slots[0].filament_type.value, expected)
    def test_existing_pla_plus_slot(self):
        # Mirrors a row previously written by unmodified scan-assign.
        with engine.begin() as c:
            c.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(42,1,'PLA+')"))
        self.assert_readable('PLA+')
    def assign(self, material, existing):
        with engine.begin() as c:
            c.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(42,'Synthetic','Test filament',:m)"), {'m':material})
            c.execute(text("INSERT INTO spools(id,filament_id,qr_code) VALUES(42,42,'test-qr')"))
            if existing:
                c.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(42,1,'empty')"))
        with SessionLocal() as db:
            response = scan_assign_spool(ScanAssignRequest(qr_code='test-qr',printer_id=42,slot=1), current_user={'role':'operator'}, db=db)
            self.assertTrue(response.success)
        # A new session must reload the written row, not use the identity cache.
        self.assert_readable('PLA+' if material=='PLA+' else 'OTHER')
        with engine.connect() as c:
            self.assertEqual(c.execute(text('SELECT material FROM filament_library WHERE id=42')).scalar_one(), material)
            self.assertEqual(c.execute(text('SELECT assigned_spool_id FROM filament_slots')).scalar_one(), 42)
    def test_new_pla_plus_assignment(self): self.assign('PLA+', False)
    def test_existing_pla_plus_assignment(self): self.assign('PLA+', True)
    def test_new_custom_material_assignment(self): self.assign('custom blend', False)
    def test_existing_custom_material_assignment(self): self.assign('custom blend', True)

if __name__ == '__main__': unittest.main(verbosity=2)
