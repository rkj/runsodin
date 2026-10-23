"""Child process for bounded sliced-3MF plate extraction.

``python -m modules.models_library.plate_worker <memory> <cpu_seconds> <parent_pid>
<archive_path> <original_filename> <destination>``

The API process never opens the uploaded archive. This child validates it with
the same ZIP bounds as every other 3MF parser, streams only canonical
``Metadata/plate_N.gcode`` member data into generated file names, and returns a
small JSON frame. Files are written only to the caller-provided directory.
"""
import json
import os
import sys

from modules.models_library.mesh_worker import _frame, _limit


def main() -> int:
    memory, cpu_seconds, parent_pid, archive, original, destination = (
        int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]),
        sys.argv[4], sys.argv[5], sys.argv[6])
    _limit(memory, cpu_seconds, parent_pid)
    from modules.models_library.plate_import import PlateImportError, extract_plates_to
    try:
        plates = extract_plates_to(archive, original, destination)
        payload = {"status": "ok", "plates": plates}
    except PlateImportError as exc:
        payload = {"status": "error", "message": str(exc)}
    except MemoryError:
        return 3
    except Exception:
        payload = {"status": "error", "message": "Unable to read sliced plate data from this archive."}
    _frame(b"P", json.dumps(payload).encode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
