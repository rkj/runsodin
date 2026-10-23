"""Printer-bound raw G-code imports stay on the exact printer selected at import.

Source-backed contract: no live HTTP, printer, SSH or transport. Wrong-printer
work is refused before credentials or an adapter are reached.
"""
import asyncio
import inspect
import io
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from starlette.datastructures import UploadFile
from starlette.requests import Request

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


def _request(path="/api/print-files/upload"):
    return Request({"type": "http", "method": "POST", "path": path, "headers": [],
                    "client": ("127.0.0.1", 1), "server": ("localhost", 80), "scheme": "http"})


@pytest.fixture()
def bound_imports(review_db, tmp_path, monkeypatch):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    if not registry.has_provider("EducationPolicyProvider"):
        registry.register_provider("EducationPolicyProvider", EducationPolicyService())
    review_db.execute(text(
        "INSERT INTO printers (id,name,api_type,machine_type,is_active,bed_x_mm,bed_y_mm,org_id) VALUES "
        "(42,'Target U1','moonraker','Snapmaker U1',1,256,256,1),"
        "(43,'Other U1','moonraker','Snapmaker U1',1,256,256,1),"
        "(44,'Prusa','prusalink','MK4',1,250,210,1),"
        "(45,'Private printer','moonraker','Snapmaker U1',1,256,256,2)"
    ))
    storage = tmp_path / "bound-storage"
    storage.mkdir()
    monkeypatch.setenv("PRINT_FILE_STORAGE_ROOT", str(storage))
    review_db.commit()
    user = {"id": 3, "username": "admin", "role": "admin", "group_id": 1, "is_active": True}
    from modules.models_library.routes import print_files
    uploader = inspect.unwrap(print_files.upload_3mf)
    paths = []

    def upload(target=42, name="test.gcode", data=b"; printer_model = Test U1\nG28\n", user_override=None):
        result = asyncio.run(uploader(
            request=_request(),
            file=UploadFile(filename=name, file=io.BytesIO(data)),
            target_printer_id=target,
            current_user=user_override or user,
            db=review_db,
        ))
        path = review_db.execute(text("SELECT stored_path FROM print_files WHERE id=:id"),
                                 {"id": result["id"]}).scalar_one()
        paths.append(path)
        return result

    yield review_db, upload, user, storage, paths


def _upload_failure(exc_type, upload, **kwargs):
    with pytest.raises(exc_type) as error:
        upload(**kwargs)
    return error.value


def test_required_printer_and_binary_format(bound_imports):
    db, upload, _, _, _ = bound_imports
    error = _upload_failure(HTTPException, upload, target=None)
    assert error.status_code == 400
    error = _upload_failure(HTTPException, upload, target=42, name="test.bgcode", data=b"GCDE")
    assert error.status_code == 400
    binary = upload(target=44, name="test.bgcode", data=b"GCDE")
    assert binary["target_printer_id"] == 44
    assert binary["target_printer_name"] == "Prusa"


def test_inaccessible_printer_makes_no_record(bound_imports):
    db, upload, _, storage, _ = bound_imports
    before = db.execute(text("SELECT count(*) FROM print_files")).scalar_one()
    error = _upload_failure(Exception, upload, target=45)
    assert getattr(error, "status", None) == 404
    assert db.execute(text("SELECT count(*) FROM print_files")).scalar_one() == before
    assert list(storage.iterdir()) == []


def test_same_name_files_keep_separate_exact_artifacts(bound_imports):
    db, upload, _, _, _ = bound_imports
    first = upload(target=42, data=b"G28\n; first artifact\n")
    second = upload(target=43, data=b"G28\n; second artifact\n")
    assert first["model_id"] != second["model_id"]
    rows = db.execute(text(
        "SELECT m.id,m.print_file_id,m.org_id,pf.org_id,pf.compatibility_facts_json,pf.stored_path "
        "FROM models m JOIN print_files pf ON pf.id=m.print_file_id "
        "WHERE m.id IN (:first,:second) ORDER BY m.id"
    ), {"first": first["model_id"], "second": second["model_id"]}).all()
    assert [(row[1], row[2], row[3]) for row in rows] == [
        (first["id"], 1, 1), (second["id"], 1, 1)]
    assert [json.loads(row[4])["target_printer_id"] for row in rows] == [42, 43]
    assert [Path(row[5]).read_bytes() for row in rows] == [
        b"G28\n; first artifact\n", b"G28\n; second artifact\n"]
    assert db.execute(text(
        "SELECT count(*) FROM jobs WHERE print_file_id IN (:first,:second)"
    ), {"first": first["id"], "second": second["id"]}).scalar_one() == 0


def test_model_schedule_uses_bound_printer_and_rejects_another(bound_imports):
    db, upload, user, _, _ = bound_imports
    imported = upload(target=42)
    from modules.models_library.routes.models_crud import schedule_from_model
    error = _upload_failure(HTTPException, schedule_from_model, model_id=imported["model_id"],
                            printer_id=43, current_user=user, db=db)
    assert error.status_code == 400
    result = schedule_from_model(model_id=imported["model_id"], printer_id=None,
                                 current_user=user, db=db)
    assert db.execute(text("SELECT printer_id FROM jobs WHERE id=:id"),
                      {"id": result["job_id"]}).scalar_one() == 42


def test_scheduler_never_places_bound_file_on_another_printer(bound_imports):
    db, upload, user, _, _ = bound_imports
    imported = upload(target=42)
    from modules.models_library.routes.models_crud import schedule_from_model
    from modules.jobs.scheduler import Scheduler
    job_id = schedule_from_model(model_id=imported["model_id"], printer_id=None,
                                 current_user=user, db=db)["job_id"]
    db.execute(text("UPDATE jobs SET status='pending', printer_id=NULL WHERE id=:id"), {"id": job_id})
    db.execute(text("UPDATE printers SET is_active=0 WHERE id IN (9,10,42)"))
    db.execute(text("UPDATE printers SET is_active=1 WHERE id=43"))
    db.commit()
    result = Scheduler().run(db)
    assert result.scheduled_count == 0
    assert db.execute(text("SELECT status,printer_id FROM jobs WHERE id=:id"),
                      {"id": job_id}).one() == ("pending", None)

    db.execute(text("UPDATE printers SET is_active=1 WHERE id=42"))
    db.execute(text("UPDATE printers SET is_active=0 WHERE id=43"))
    db.commit()
    result = Scheduler().run(db)
    assert result.scheduled_count == 1
    assert db.execute(text("SELECT printer_id FROM jobs WHERE id=:id"),
                      {"id": job_id}).scalar_one() == 42


def test_dispatch_rejects_reassigned_job_before_credentials_or_transport(bound_imports, monkeypatch):
    db, upload, user, _, _ = bound_imports
    imported = upload(target=42)
    from modules.models_library.routes.models_crud import schedule_from_model
    from modules.printers import dispatch
    job_id = schedule_from_model(model_id=imported["model_id"], printer_id=None,
                                 current_user=user, db=db)["job_id"]
    db.execute(text("UPDATE jobs SET printer_id=43,status='scheduled' WHERE id=:id"), {"id": job_id})
    db.commit()

    from core import db as core_db
    monkeypatch.setattr(core_db, "engine", db.get_bind())

    def forbidden(*args, **kwargs):
        raise AssertionError("credentials or transport must not be reached")

    monkeypatch.setattr(dispatch, "_get_printer_info", forbidden)
    monkeypatch.setattr(dispatch, "_dispatch_moonraker", forbidden)
    ok, message = dispatch.dispatch_job(43, job_id)
    assert ok is False
    assert "another printer" in message


def test_dispatch_loads_the_exact_selected_file_not_a_same_name_variant(bound_imports, monkeypatch):
    db, upload, user, _, paths = bound_imports
    first = upload(target=42, data=b"G28\n; exact first\n")
    second = upload(target=43, data=b"G28\n; other variant\n")
    from modules.models_library.routes.models_crud import schedule_from_model
    from modules.printers import dispatch
    from core import db as core_db
    job_id = schedule_from_model(model_id=first["model_id"], printer_id=None,
                                 current_user=user, db=db)["job_id"]
    db.execute(text("UPDATE jobs SET status='scheduled' WHERE id=:id"), {"id": job_id})
    # A later primary/variant change cannot redirect the job's chosen file.
    db.execute(text("UPDATE models SET print_file_id=:other WHERE id=:model"),
               {"other": second["id"], "model": first["model_id"]})
    db.commit()
    monkeypatch.setattr(core_db, "engine", db.get_bind())
    loaded = dispatch._load_job(job_id)
    assert loaded["print_file_id"] == first["id"]
    assert loaded["stored_path"] == paths[0]
    assert Path(loaded["stored_path"]).read_bytes() == b"G28\n; exact first\n"


def test_generic_file_schedule_and_studio_enforce_binding(bound_imports):
    db, upload, user, _, _ = bound_imports
    imported = upload(target=42)
    from modules.models_library.file_management import schedule_generic_file
    error = _upload_failure(HTTPException, schedule_generic_file, db=db, file_id=imported["id"],
                            printer_id=43, actor=user)
    assert error.status_code == 400
    result = schedule_generic_file(db, imported["id"], None, user)
    assert db.execute(text("SELECT printer_id FROM jobs WHERE id=:id"),
                      {"id": result["job_id"]}).scalar_one() == 42

    # The generic Studio route binds its own upload and refuses a row that lost it.
    studio_import = upload(target=42, name="studio.gcode", data=b"G28\n; studio\n")
    db.execute(text("UPDATE print_files SET compatibility_facts_json='{}' WHERE id=:id"),
               {"id": studio_import["id"]})
    db.commit()
    from modules.models_library.routes import print_files
    source = inspect.getsource(print_files.submit_studio_print)
    assert "target_printer_id=target_printer_id" in source
    assert "bound_printer_id != target_printer_id" in source
