"""Exact physical-printer association for locally imported, pre-sliced files.

The association is evidence stored with the imported file itself
(``print_files.compatibility_facts_json.target_printer_id``). It is never a
plural model-level setting: a model can own several same-name variants, so the
canonical model file (``models.print_file_id``) is the only file a model-level
lookup may use.
"""
import json

from sqlalchemy import text


def target_from_facts(raw):
    if not raw:
        return None
    try:
        facts = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid compatibility facts") from exc
    if not isinstance(facts, dict):
        raise ValueError("Invalid compatibility facts")
    target = facts.get("target_printer_id")
    if target is not None and (type(target) is not int or target < 1):
        raise ValueError("Invalid target printer")
    return target


def model_target_printer(db, model_id):
    """Association of the model's exact canonical file, or None."""
    if not model_id:
        return None
    # The model's selected file is canonical; pf.model_id can point at another
    # same-name variant and must not authorize dispatch for this model.
    row = db.execute(
        text("SELECT pf.compatibility_facts_json FROM models m "
             "JOIN print_files pf ON pf.id=m.print_file_id WHERE m.id=:id"),
        {"id": model_id},
    ).fetchone()
    return target_from_facts(row[0]) if row else None


def job_target_printer(db, job_id):
    """Association of the exact file dispatch would use for this job, or None.

    Uses the same selected-file rule as ``core.dispatch_payload_selection`` so a
    stale variant can neither impose nor evade the association.
    """
    if not job_id:
        return None
    from core.dispatch_payload_selection import SELECTED_PRINT_FILE_ID_SQL
    row = db.execute(
        text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant selected-file expression; values are bound
            "SELECT pf.compatibility_facts_json FROM jobs j "
            "LEFT JOIN education_submissions s ON s.job_id=j.id "
            "LEFT JOIN models m ON m.id=j.model_id "
            "LEFT JOIN print_files pf ON pf.id=(" + SELECTED_PRINT_FILE_ID_SQL + ") "
            "WHERE j.id=:id"
        ),
        {"id": job_id},
    ).fetchone()
    return target_from_facts(row[0]) if row else None


def assert_model_printer_binding(db, model_id, printer_id):
    """Reject an explicit printer choice that contradicts the file's import binding."""
    if model_id is None or printer_id is None:
        return
    from fastapi import HTTPException
    try:
        bound_printer_id = model_target_printer(db, model_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file printer association; re-import the file") from None
    if bound_printer_id is not None and bound_printer_id != printer_id:
        raise HTTPException(
            status_code=400,
            detail="This file is associated with a different printer; import a separately sliced file",
        )


