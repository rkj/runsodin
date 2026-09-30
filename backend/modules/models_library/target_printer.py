"""Exact physical-printer association for locally imported, pre-sliced files."""
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
    if not model_id:
        return None
    row = db.execute(text("SELECT pf.compatibility_facts_json FROM models m JOIN print_files pf ON pf.id=m.print_file_id WHERE m.id=:id"), {"id": model_id}).fetchone()
    return target_from_facts(row[0]) if row else None
