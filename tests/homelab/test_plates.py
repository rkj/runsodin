"""Sliced-plate extraction and atomic API import; never contacts printer hardware."""
import asyncio
from io import BytesIO
import inspect
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

sys.path.insert(0, str(Path(__file__).parent))
try:
    import plate_import as plate_helper
except ModuleNotFoundError:
    sys.path.insert(0, os.getenv("ODIN_TEST_BACKEND", "/app/backend"))
    from modules.models_library import plate_import as plate_helper
extracted_plates = plate_helper.extracted_plates
PlateImportError = plate_helper.PlateImportError


def archive_bytes(entries):
    stream = BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, data in entries:
                zf.writestr(name, data)
    return stream.getvalue()


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.archive = Path(self.tmp.name) / "synthetic.3mf"

    def tearDown(self):
        self.tmp.cleanup()

    def extract(self, entries):
        self.archive.write_bytes(archive_bytes(entries))
        return extracted_plates(self.archive, "synthetic-project.3mf")

    def test_sparse_plates_sorted_and_bytes_preserved(self):
        with self.extract([("Metadata/plate_7.gcode", b"G1 X7\n"),
                           ("Metadata/plate_2.gcode", b"G1 X2\n")]) as plates:
            self.assertEqual([p.number for p in plates], [2, 7])
            self.assertEqual([p.path.read_bytes() for p in plates], [b"G1 X2\n", b"G1 X7\n"])
            self.assertEqual(plates[0].filename, "synthetic-project_plate_2.gcode")
            extracted_path = plates[0].path
        self.assertFalse(extracted_path.exists())

    def test_unsliced_explains_required_export(self):
        with self.assertRaisesRegex(PlateImportError, "no sliced plate G-code"):
            with self.extract([("3D/3dmodel.model", b"<model/>")]):
                pass

    def test_full_archive_rejects_unsafe_unrelated_member(self):
        for name in ("../secret", "/absolute", "Metadata\\plate_3.gcode", "x:evil", "Metadata//plate_1.gcode"):
            with self.subTest(name=name), self.assertRaises(PlateImportError):
                with self.extract([("Metadata/plate_1.gcode", b"G1 X1\n"), (name, b"x")]):
                    pass

    def test_duplicates_and_empty_plate_rejected(self):
        for entries in (
            [("Metadata/plate_1.gcode", b"G1"), ("Metadata/plate_01.gcode", b"G2")],
            [("Metadata/plate_1.gcode", b"G1"), ("metadata/PLATE_1.GCODE", b"G2")],
            [("Metadata/plate_1.gcode", b"")],
            [("Metadata/plate_0.gcode", b"G1")],
        ):
            with self.subTest(entries=len(entries)), self.assertRaises(PlateImportError):
                with self.extract(entries):
                    pass

    def test_size_plate_and_member_limits(self):
        entries = [("Metadata/plate_1.gcode", b"G1 X1\n"), ("Metadata/plate_3.gcode", b"G1 X3\n")]
        for constant, limit in (("MAX_ARCHIVE_BYTES", 1), ("MAX_TOTAL_BYTES", 2),
                                ("MAX_PLATE_BYTES", 2), ("MAX_PLATES", 1), ("MAX_MEMBERS", 1)):
            with self.subTest(limit=constant), patch.object(plate_helper, constant, limit), self.assertRaises(PlateImportError):
                with self.extract(entries):
                    pass

    def test_symbolic_link_member_rejected(self):
        item = zipfile.ZipInfo("Metadata/plate_1.gcode")
        item.create_system = 3
        item.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(PlateImportError, "symbolic link"):
            with self.extract([(item, b"/etc/passwd")]):
                pass

    def test_bad_archive_rejected(self):
        self.archive.write_bytes(b"not-a-zip")
        with self.assertRaisesRegex(PlateImportError, "bad zip"):
            with extracted_plates(self.archive, "synthetic.3mf"):
                pass


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The image's installed models + migration ledger define the schema.
        backend = os.getenv("ODIN_TEST_BACKEND", "/app/backend")
        sys.path.insert(0, backend)
        cls.root = tempfile.TemporaryDirectory(prefix="odin-plate-tests-")
        os.environ["JWT_SECRET_KEY"] = "synthetic-plate-test-secret-no-live-credentials-1234567890"
        os.environ["DATABASE_URL"] = "sqlite:///" + cls.root.name + "/config.db"
        from sqlalchemy import create_engine
        from core.schema.bootstrap import bootstrap_database
        cls.engine = create_engine("sqlite:///" + cls.root.name + "/actual.db")
        bootstrap_database(cls.engine, backend_root=Path(backend))
        from modules.models_library.routes import print_files
        cls.routes = print_files
        from sqlalchemy import text
        with cls.engine.begin() as db:
            db.execute(text("INSERT INTO printers (id,name,api_type,machine_type,org_id) VALUES (1,'Synthetic U1','moonraker','Snapmaker U1',7)"))
        cls.storage = Path(cls.root.name) / "files"
        cls.storage.mkdir()
        cls.original_mkdtemp = staticmethod(tempfile.mkdtemp)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        cls.root.cleanup()

    def setUp(self):
        from sqlalchemy.orm import Session
        self.db = Session(self.engine)
        self.request = self.new_request()
        self.user = {"id": 1, "role": "operator", "group_id": 7}
        self.fn = inspect.unwrap(self.routes.import_sliced_plates)

    @staticmethod
    def new_request():
        from starlette.requests import Request
        return Request({"type": "http", "method": "POST", "path": "/print-files/import-plates", "headers": [], "client": ("127.0.0.1", 1), "server": ("localhost", 80), "scheme": "http"})

    def tearDown(self):
        self.db.close()
        from sqlalchemy import text
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM print_files"))
            db.execute(text("DELETE FROM models"))
        import shutil
        for path in self.storage.iterdir():
            shutil.rmtree(path)

    def run_import(self, entries, **overrides):
        from fastapi import UploadFile
        kwargs = dict(request=self.request,
                      file=UploadFile(filename="synthetic.3mf", file=BytesIO(archive_bytes(entries))),
                      target_printer_id=1, current_user=self.user, db=self.db)
        kwargs.update(overrides)
        # Only storage location is redirected; real parser/uploader and real schema remain.
        def batch_directory(*args, **kwargs):
            if kwargs.get("prefix") == "plates-":
                kwargs["dir"] = str(self.storage)
            return self.original_mkdtemp(*args, **kwargs)
        with patch.object(self.routes.tempfile, "mkdtemp", side_effect=batch_directory):
            return asyncio.run(self.fn(**kwargs))

    def test_real_schema_import_binds_each_sparse_plate_and_preserves_content(self):
        from sqlalchemy import text
        content2 = b"; generated by OrcaSlicer\n; printer_model = Snapmaker U1\nG1 X2\n"
        content7 = b"; generated by OrcaSlicer\n; printer_model = Snapmaker U1\nG1 X7\n"
        response = self.run_import([("Metadata/plate_7.gcode", content7), ("Metadata/plate_2.gcode", content2)])
        self.assertEqual(response["plate_count"], 2)
        files = response["files"]
        self.assertEqual([f["source_plate"] for f in files], [2, 7])
        self.assertEqual([f["target_printer_id"] for f in files], [1, 1])
        self.assertEqual(len(set(f["model_id"] for f in files)), 2)
        rows = self.db.execute(text("SELECT pf.stored_path,pf.compatibility_facts_json,pf.org_id,m.org_id FROM print_files pf JOIN models m ON m.print_file_id=pf.id ORDER BY pf.id")).fetchall()
        self.assertEqual([Path(row[0]).read_bytes() for row in rows], [content2, content7])
        self.assertEqual([json.loads(row[1])["target_printer_id"] for row in rows], [1, 1])
        self.assertEqual([(row[2],row[3]) for row in rows], [(7,7),(7,7)])
        self.assertNotIn("stored_path", files[0])
        self.assertEqual(self.db.execute(text("SELECT count(*) FROM jobs")).scalar(), 0)

    def test_failed_second_plate_rolls_back_rows_and_all_copied_files(self):
        from sqlalchemy import text
        real = inspect.unwrap(self.routes.upload_3mf)
        calls = []
        async def fail_after_real_upload(**kwargs):
            result = await real(**kwargs)
            calls.append(result)
            if len(calls) == 2:
                raise RuntimeError("synthetic second-plate failure")
            return result
        with patch.object(self.routes, "upload_3mf", fail_after_real_upload):
            with self.assertRaisesRegex(RuntimeError, "second-plate"):
                self.run_import([("Metadata/plate_1.gcode", b"G1 X1\n"), ("Metadata/plate_2.gcode", b"G1 X2\n")])
        self.assertEqual(self.db.execute(text("SELECT count(*) FROM print_files")).scalar(), 0)
        self.assertEqual(self.db.execute(text("SELECT count(*) FROM models")).scalar(), 0)
        self.assertEqual(list(self.storage.iterdir()), [])

    def test_partial_second_copy_is_cleaned_and_database_rolled_back(self):
        from sqlalchemy import text
        import shutil
        copy = shutil.copy2
        calls = []
        def fail_second_copy(source, destination, *args, **kwargs):
            calls.append(destination)
            if len(calls) == 2:
                Path(destination).write_bytes(b"synthetic partial file")
                raise OSError("synthetic disk write failure")
            return copy(source, destination, *args, **kwargs)
        with patch.object(shutil, "copy2", side_effect=fail_second_copy):
            with self.assertRaisesRegex(OSError, "disk write failure"):
                self.run_import([("Metadata/plate_1.gcode", b"G1 X1\n"), ("Metadata/plate_2.gcode", b"G1 X2\n")])
        self.assertEqual(self.db.execute(text("SELECT count(*) FROM print_files")).scalar(), 0)
        self.assertEqual(self.db.execute(text("SELECT count(*) FROM models")).scalar(), 0)
        self.assertEqual(list(self.storage.iterdir()), [])

    def test_unsliced_archive_and_wrong_org_make_no_records(self):
        from fastapi import HTTPException
        from sqlalchemy import text
        with self.assertRaises(HTTPException) as exc:
            self.run_import([("3D/3dmodel.model", b"<model/>")])
        self.assertEqual(exc.exception.status_code, 400)
        with self.assertRaises(HTTPException):
            self.run_import([("Metadata/plate_1.gcode", b"G1 X1\n"), ("../unsafe", b"x")])
        with self.assertRaises(Exception) as exc:
            self.run_import([("Metadata/plate_1.gcode", b"G1 X1\n")], current_user={"id":2,"role":"operator","group_id":8})
        self.assertEqual(getattr(exc.exception, "status_code", getattr(exc.exception, "status", None)), 404)
        self.assertEqual(self.db.execute(text("SELECT count(*) FROM print_files")).scalar(), 0)
        self.assertEqual(list(self.storage.iterdir()), [])


if __name__ == "__main__":
    if os.getenv("ODIN_PLATE_EXTRACTION_ONLY"):
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(ExtractionTests)
        unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() or sys.exit(1)
    else:
        unittest.main(verbosity=2)
