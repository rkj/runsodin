"""Sliced-3MF plate extraction is bounded, isolated, and atomically imported."""
import asyncio
import inspect
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import warnings
import zipfile

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from starlette.datastructures import UploadFile
from starlette.requests import Request

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


def archive_bytes(entries):
    stream = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as package:
            for name, data in entries:
                package.writestr(name, data)
    return stream.getvalue()


@pytest.fixture()
def archive(tmp_path):
    path = tmp_path / "synthetic.3mf"
    return path


def extract(plate_import, archive, entries):
    archive.write_bytes(archive_bytes(entries))
    with plate_import.extracted_plates(archive, "synthetic-project.3mf") as plates:
        return [(plate.number, plate.filename, plate.path.read_bytes()) for plate in plates]


def test_sparse_plates_sorted_and_bytes_preserved(archive):
    from modules.models_library import plate_import
    archive.write_bytes(archive_bytes([
        ("Metadata/plate_7.gcode", b"G1 X7\n"),
        ("Metadata/plate_2.gcode", b"G1 X2\n"),
    ]))
    with plate_import.extracted_plates(archive, "synthetic-project.3mf") as plates:
        assert [plate.number for plate in plates] == [2, 7]
        assert [plate.path.read_bytes() for plate in plates] == [b"G1 X2\n", b"G1 X7\n"]
        assert [plate.filename for plate in plates] == [
            "synthetic-project_plate_2.gcode", "synthetic-project_plate_7.gcode"]
        first_path = plates[0].path
    assert not first_path.exists()


def test_unsliced_archive_explains_required_export(archive):
    from modules.models_library import plate_import
    archive.write_bytes(archive_bytes([("3D/3dmodel.model", b"<model/>")]))
    with pytest.raises(plate_import.PlateImportError, match="no sliced plate G-code"):
        with plate_import.extracted_plates(archive, "synthetic.3mf"):
            pass


@pytest.mark.parametrize("unsafe", [
    "../secret", "/absolute", "Metadata\\plate_3.gcode", "x:evil", "Metadata//plate_1.gcode",
])
def test_full_archive_rejects_unsafe_unrelated_member(archive, unsafe):
    from modules.models_library import plate_import
    archive.write_bytes(archive_bytes([
        ("Metadata/plate_1.gcode", b"G1 X1\n"), (unsafe, b"x")]))
    with pytest.raises(plate_import.PlateImportError, match="unsafe|non-canonical"):
        with plate_import.extracted_plates(archive, "synthetic.3mf"):
            pass


@pytest.mark.parametrize("entries", [
    [("Metadata/plate_1.gcode", b"G1"), ("Metadata/plate_01.gcode", b"G2")],
    [("Metadata/plate_1.gcode", b"G1"), ("metadata/PLATE_1.GCODE", b"G2")],
    [("Metadata/plate_1.gcode", b"")],
    [("Metadata/plate_0.gcode", b"G1")],
])
def test_duplicate_and_invalid_plate_numbers_rejected(archive, entries):
    from modules.models_library import plate_import
    archive.write_bytes(archive_bytes(entries))
    with pytest.raises(plate_import.PlateImportError):
        with plate_import.extracted_plates(archive, "synthetic.3mf"):
            pass


def test_size_plate_and_member_limits(archive, tmp_path, monkeypatch):
    # Limits are enforced inside the isolated worker; call that exact function
    # here so the test can shrink each constant without a large fixture.
    from modules.models_library import plate_import
    destination = tmp_path / "bounded-out"
    destination.mkdir()
    entries = [("Metadata/plate_1.gcode", b"G1 X1\n"), ("Metadata/plate_3.gcode", b"G1 X3\n")]
    for constant, limit in (("MAX_ARCHIVE_BYTES", 1), ("MAX_TOTAL_BYTES", 2),
                            ("MAX_PLATE_BYTES", 2), ("MAX_PLATES", 1), ("MAX_MEMBERS", 1)):
        with monkeypatch.context() as scoped:
            scoped.setattr(plate_import, constant, limit)
            archive.write_bytes(archive_bytes(entries))
            with pytest.raises(plate_import.PlateImportError):
                plate_import.extract_plates_to(archive, "synthetic.3mf", destination)


def test_symbolic_link_member_rejected(archive):
    from modules.models_library import plate_import
    item = zipfile.ZipInfo("Metadata/plate_1.gcode")
    item.create_system = 3
    item.external_attr = (stat.S_IFLNK | 0o777) << 16
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as package:
        package.writestr(item, b"/etc/passwd")
    archive.write_bytes(stream.getvalue())
    with pytest.raises(plate_import.PlateImportError, match="symbolic link"):
        with plate_import.extracted_plates(archive, "synthetic.3mf"):
            pass


def test_bad_archive_rejected(archive):
    from modules.models_library import plate_import
    archive.write_bytes(b"not-a-zip")
    with pytest.raises(plate_import.PlateImportError, match="bad zip"):
        with plate_import.extracted_plates(archive, "synthetic.3mf"):
            pass


@pytest.fixture()
def plate_route(review_db, tmp_path, monkeypatch):
    review_db.execute(text(
        "INSERT INTO printers (id,name,api_type,machine_type,is_active,bed_x_mm,bed_y_mm,org_id) "
        "VALUES (42,'Target U1','moonraker','Snapmaker U1',1,256,256,1)"))
    review_db.commit()
    storage = tmp_path / "plate-storage"
    storage.mkdir()
    monkeypatch.setenv("PRINT_FILE_STORAGE_ROOT", str(storage))
    from modules.models_library.routes import print_files
    from modules.models_library.routes import plate_imports
    route = inspect.unwrap(plate_imports.import_sliced_plates)
    request = Request({"type": "http", "method": "POST", "path": "/api/print-files/import-plates",
                       "headers": [], "client": ("127.0.0.1", 1), "server": ("localhost", 80),
                       "scheme": "http"})
    user = {"id": 3, "username": "admin", "role": "admin", "group_id": 1, "is_active": True}

    def run(entries, *, target=42, user_override=None):
        data = archive_bytes(entries)
        return asyncio.run(route(
            request=request,
            file=UploadFile(filename="synthetic.3mf", file=io.BytesIO(data)),
            target_printer_id=target,
            current_user=user_override or user,
            db=review_db,
        ))

    yield review_db, run, storage
    for child in storage.iterdir():
        shutil.rmtree(child)


def test_plate_route_binds_each_sparse_plate_and_preserves_content(plate_route):
    db, run, _ = plate_route
    content_two = b"; generated by OrcaSlicer\nG1 X2\n"
    content_seven = b"; generated by OrcaSlicer\nG1 X7\n"
    response = run([("Metadata/plate_7.gcode", content_seven),
                    ("Metadata/plate_2.gcode", content_two)])
    assert response["plate_count"] == 2
    files = response["files"]
    assert [item["source_plate"] for item in files] == [2, 7]
    assert [item["target_printer_id"] for item in files] == [42, 42]
    assert len({item["model_id"] for item in files}) == 2
    rows = db.execute(text(
        "SELECT pf.stored_path,pf.compatibility_facts_json,pf.org_id,m.org_id "
        "FROM print_files pf JOIN models m ON m.print_file_id=pf.id "
        "WHERE pf.id IN (:first,:second) ORDER BY pf.id"
    ), {"first": files[0]["id"], "second": files[1]["id"]}).all()
    assert [Path(row[0]).read_bytes() for row in rows] == [content_two, content_seven]
    assert [json.loads(row[1])["target_printer_id"] for row in rows] == [42, 42]
    assert [(row[2], row[3]) for row in rows] == [(1, 1), (1, 1)]
    assert "stored_path" not in files[0]
    assert db.execute(text("SELECT count(*) FROM jobs WHERE print_file_id IN (:first,:second)"),
                      {"first": files[0]["id"], "second": files[1]["id"]}).scalar_one() == 0


def test_plate_route_failed_second_plate_rolls_back_and_removes_copied_files(plate_route):
    db, run, storage = plate_route
    from modules.models_library.routes import print_files
    real = inspect.unwrap(print_files.upload_3mf)
    calls = []

    async def fail_after_real_upload(**kwargs):
        result = await real(**kwargs)
        calls.append(result)
        if len(calls) == 2:
            raise RuntimeError("synthetic second-plate failure")
        return result

    original = print_files.upload_3mf
    print_files.upload_3mf = fail_after_real_upload
    try:
        with pytest.raises(RuntimeError, match="second-plate"):
            run([("Metadata/plate_1.gcode", b"G1 X1\n"),
                 ("Metadata/plate_2.gcode", b"G1 X2\n")])
    finally:
        print_files.upload_3mf = original
    # The pre-existing review fixture owns ids 11/12; this import added nothing.
    assert db.execute(text("SELECT count(*) FROM print_files WHERE id > 11")).scalar_one() == 0
    assert db.execute(text("SELECT count(*) FROM models WHERE id > 12")).scalar_one() == 0
    assert list(storage.iterdir()) == []


def test_plate_route_unsliced_unsafe_and_wrong_org_make_no_records(plate_route):
    db, run, storage = plate_route
    before_files = db.execute(text("SELECT count(*) FROM print_files")).scalar_one()
    before_models = db.execute(text("SELECT count(*) FROM models")).scalar_one()
    with pytest.raises(HTTPException) as exc:
        run([("3D/3dmodel.model", b"<model/>")])
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        run([("Metadata/plate_1.gcode", b"G1 X1\n"), ("../unsafe", b"x")])
    assert exc.value.status_code == 400
    with pytest.raises(Exception) as exc:
        run([("Metadata/plate_1.gcode", b"G1 X1\n")],
            user_override={"id": 5, "username": "foreign", "role": "admin", "group_id": 2,
                           "is_active": True})
    assert getattr(exc.value, "status", None) == 404
    assert db.execute(text("SELECT count(*) FROM print_files")).scalar_one() == before_files
    assert db.execute(text("SELECT count(*) FROM models")).scalar_one() == before_models
    assert list(storage.iterdir()) == []
