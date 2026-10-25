"""Atomic sliced-plate imports, separate from ordinary upload routes."""
import os
import tempfile
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session
from core.db import get_db
from core.rate_limit import limiter
from core.rbac import require_action

router = APIRouter(tags=["Print Files"])

@router.post("/print-files/import-plates")
@limiter.limit("10/minute")
async def import_sliced_plates(
    request: Request,
    file: UploadFile = File(...),
    target_printer_id: int = Form(...),
    current_user: dict = Depends(require_action("upload.upload", scope="write:models")),
    db: Session = Depends(get_db),
):
    """Import every sliced 3MF plate separately; never dispatch printer hardware.

    The archive is validated and extracted in the isolated 3MF worker before any
    database write. Each plate then uses the ordinary G-code upload path through
    one scoped batch session: inner commits only flush, and the request commits
    once. Files live in that batch directory, which is removed only when the
    transaction is known not to have committed.
    """
    import inspect
    from modules.models_library.routes.print_files import _get_accessible_printer, upload_3mf
    import shutil
    from starlette.concurrency import run_in_threadpool
    from modules.models_library.mesh_isolation import Busy
    from modules.models_library.plate_import import extract_plates_into, PlateImportError
    from modules.models_library.upload_storage import _upload_storage_root

    filename = file.filename or ""
    if os.path.splitext(filename)[1].lower() != ".3mf":
        raise HTTPException(status_code=400, detail="Plate import requires a sliced .3mf file")
    _get_accessible_printer(db, target_printer_id, current_user)
    content = await file.read(100 * 1024 * 1024 + 1)
    if len(content) > 100 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large. Maximum upload size is 100 MB.")

    class BatchSession:
        """Defer the uploader's commit to the request transaction only."""

        def __init__(self, session, directory):
            self.session = session
            self.print_file_dir = directory
            self.outcome_uncertain = False

        def __getattr__(self, name):
            return getattr(self.session, name)

        def _flush(self):
            try:
                self.session.flush()
            except BaseException:
                # The request may already have committed part of this batch.
                self.outcome_uncertain = True
                raise

        def commit(self):
            self._flush()

        def flush(self):
            self._flush()

    archive_path = extraction_dir = batch_dir = None
    batch_db = None
    retain_batch = False
    keep_batch = False
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".3mf") as archive:
            archive.write(content)
            archive_path = archive.name
        extraction_dir = tempfile.mkdtemp(prefix="odin-plate-extract-")
        plates = await run_in_threadpool(extract_plates_into, archive_path, filename, extraction_dir)

        batch_dir = tempfile.mkdtemp(prefix="plates-", dir=str(_upload_storage_root()))
        batch_db = BatchSession(db, batch_dir)
        uploader = inspect.unwrap(upload_3mf)
        files = []
        for plate in plates:
            with plate.path.open("rb") as stream:
                imported = await uploader(
                    request=request,
                    file=UploadFile(filename=plate.filename, file=stream),
                    target_printer_id=target_printer_id,
                    current_user=current_user,
                    db=batch_db,
                )
            imported["source_plate"] = plate.number
            imported["source_project"] = filename.replace("\\", "/").rsplit("/", 1)[-1]
            files.append(imported)
        try:
            db.commit()
        except BaseException:
            retain_batch = True
            try:
                db.rollback()
            except Exception:
                pass
            raise HTTPException(
                status_code=500,
                detail="Plate import outcome is uncertain. Contact support before retrying.",
            ) from None
        keep_batch = True
        return {
            "files": files,
            "plate_count": len(files),
            "target_printer_id": target_printer_id,
        }
    except Busy as busy:
        try:
            db.rollback()
        except Exception:
            retain_batch = True
        raise HTTPException(status_code=429, detail="3MF analysis is busy; try again shortly",
                            headers={"Retry-After": str(busy.retry_after)}) from None
    except PlateImportError as exc:
        try:
            db.rollback()
        except Exception:
            retain_batch = True
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BaseException:
        try:
            db.rollback()
        except Exception:
            retain_batch = True
        if batch_db is not None and batch_db.outcome_uncertain:
            retain_batch = True
        raise
    finally:
        if archive_path is not None:
            os.unlink(archive_path)
        if extraction_dir is not None:
            shutil.rmtree(extraction_dir, ignore_errors=True)
        if batch_dir is not None and not retain_batch and not keep_batch:
            shutil.rmtree(batch_dir, ignore_errors=True)


