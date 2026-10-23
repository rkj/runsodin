"""Bounded extraction of sliced Orca/Bambu 3MF plates; never executes G-code.

The uploaded package is attacker-shaped. Parsing runs in the same bounded child
process boundary as every other untrusted 3MF upload (``mesh_isolation``): the
API process never opens the archive. Only member *data* is read, never
``ZipFile.extract``/``extractall``. Generated file names are unrelated to zip
member paths, so an archive can never choose a destination.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import json
import os
import re
import tempfile
import stat
import zipfile

from core.zip_bounds import MAX_MEMBERS, ArchiveRefused, check_expansion, open_bounded
from modules.models_library import mesh_isolation

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_PLATE_BYTES = 100 * 1024 * 1024
MAX_PLATES = 100
MAX_RESULT_BYTES = 64 * 1024
_PLATE = re.compile(r"Metadata/plate_([0-9]+)\.gcode", re.IGNORECASE)
_WORKER = "modules.models_library.plate_worker"


class PlateImportError(ValueError):
    pass


@dataclass(frozen=True)
class SlicedPlate:
    number: int
    filename: str
    path: Path


def plate_filename(original_filename, number: int) -> str:
    """Presentation-only name derived from the upload name, never a zip path."""
    original = str(original_filename or "project.3mf").replace("\\", "/").rsplit("/", 1)[-1]
    stem = re.sub(r"[^a-zA-Z0-9._-]", "_", Path(original).stem)[:150] or "project"
    return f"{stem}_plate_{int(number)}.gcode"


def _valid_member_name(name: str) -> bool:
    parts = PurePosixPath(name).parts
    if "\\" in name or name.startswith("/") or ".." in parts:
        return False
    if any(":" in part for part in parts):
        return False
    return True


def extract_plates_to(archive_path, original_filename, destination) -> list:
    """Validate the whole archive, then write sorted plate files into *destination*.

    This function is only called from the isolated worker child. It reads member
    data without extracting archive paths and bounds every declared and observed
    size before writing a generated file.
    """
    archive = Path(archive_path)
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise PlateImportError("File too large. Maximum upload size is 100 MB.")
    try:
        package = open_bounded(archive, max_members=MAX_MEMBERS)
    except ArchiveRefused as refused:
        if refused.reason in ("too_large", "member_too_large"):
            raise PlateImportError("File rejected: decompressed size exceeds 500 MB limit.") from None
        raise PlateImportError("Archive contains too many entries.") from None
    except zipfile.BadZipFile as exc:
        raise PlateImportError("Invalid .3mf file (bad zip structure).") from exc
    with package:
        try:
            check_expansion(package)
        except ArchiveRefused as refused:
            if refused.reason in ("too_large", "member_too_large"):
                raise PlateImportError("File rejected: decompressed size exceeds 500 MB limit.") from None
            raise PlateImportError("Archive contains too many entries.") from None
        entries = package.infolist()
        if len(entries) > MAX_MEMBERS:
            raise PlateImportError("Archive contains too many entries.")
        if sum(item.file_size for item in entries) > MAX_TOTAL_BYTES:
            raise PlateImportError("File rejected: decompressed size exceeds 500 MB limit.")
        seen_names, seen_plates, members = set(), set(), []
        for item in entries:
            name = item.filename
            if not _valid_member_name(name):
                raise PlateImportError("Archive contains an unsafe member path.")
            if name.endswith("/"):
                continue  # explicit directory entries carry no payload
            canonical = str(PurePosixPath(name))
            if name != canonical:
                raise PlateImportError("Archive contains a non-canonical member path.")
            mode = (item.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise PlateImportError("Archive contains a symbolic link member.")
            if canonical.casefold() in seen_names:
                raise PlateImportError("Archive contains duplicate member names.")
            seen_names.add(canonical.casefold())
            if item.flag_bits & 1:
                raise PlateImportError("Encrypted .3mf files are not supported.")
            match = _PLATE.fullmatch(name)
            if not match:
                continue
            digits = match.group(1)
            if len(digits) > 9 or int(digits) <= 0:
                raise PlateImportError("Archive contains an invalid plate number.")
            number = int(digits)
            if number in seen_plates:
                raise PlateImportError("Archive contains duplicate plate numbers.")
            seen_plates.add(number)
            if item.file_size <= 0 or item.file_size > MAX_PLATE_BYTES:
                raise PlateImportError("Each sliced plate must contain 1 byte to 100 MB of G-code.")
            members.append((number, item))
        if not members:
            raise PlateImportError(
                "This project has no sliced plate G-code. Slice it in your printer's "
                "slicer and export a sliced .3mf or individual .gcode files."
            )
        if len(members) > MAX_PLATES:
            raise PlateImportError("Import at most 100 sliced plates at a time.")
        destination = Path(destination)
        plates = []
        for number, member in sorted(members, key=lambda pair: pair[0]):
            filename = plate_filename(original_filename, number)
            path = destination / filename
            size = 0
            try:
                with package.open(member) as source, path.open("xb") as target:
                    while chunk := source.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_PLATE_BYTES or size > member.file_size:
                            raise PlateImportError("Plate decompression exceeded its declared size.")
                        target.write(chunk)
            except PlateImportError:
                raise
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
                raise PlateImportError("Unable to read sliced plate data from this archive.") from exc
            if size != member.file_size:
                raise PlateImportError("Incomplete sliced plate data.")
            plates.append({"number": number, "size": size})
        return plates


def _run_isolated(archive_path, original_filename, destination) -> list:
    blob, clean_exit = mesh_isolation.run_isolated_worker(
        _WORKER,
        [str(os.getpid()), str(archive_path), str(original_filename or ""), str(destination)],
    )
    if not clean_exit:
        raise PlateImportError("Unable to read sliced plate data from this archive.")
    payload = mesh_isolation.read_frames(blob).get(b"P")
    if payload is None or len(payload) > MAX_RESULT_BYTES:
        raise PlateImportError("Unable to read sliced plate data from this archive.")
    try:
        result = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise PlateImportError("Unable to read sliced plate data from this archive.") from exc
    if not isinstance(result, dict) or result.get("status") not in {"ok", "error"}:
        raise PlateImportError("Unable to read sliced plate data from this archive.")
    if result["status"] == "error":
        message = result.get("message")
        if not isinstance(message, str) or not 0 < len(message) <= 300:
            message = "Unable to read sliced plate data from this archive."
        raise PlateImportError(message)
    plates = result.get("plates")
    if not isinstance(plates, list) or not plates or len(plates) > MAX_PLATES:
        raise PlateImportError("Unable to read sliced plate data from this archive.")
    resolved_destination = Path(destination).resolve()
    imported = []
    previous = 0
    for item in plates:
        if not isinstance(item, dict) or set(item) != {"number", "size"}:
            raise PlateImportError("Unable to read sliced plate data from this archive.")
        number, size = item["number"], item["size"]
        if type(number) is not int or type(size) is not int or not 0 < number <= 10**9:
            raise PlateImportError("Unable to read sliced plate data from this archive.")
        if number <= previous or size <= 0 or size > MAX_PLATE_BYTES:
            raise PlateImportError("Unable to read sliced plate data from this archive.")
        previous = number
        filename = plate_filename(original_filename, number)
        candidate = Path(destination) / filename
        if os.path.islink(candidate):
            raise PlateImportError("Unable to read sliced plate data from this archive.")
        path = candidate.resolve()
        if path.parent != resolved_destination or not path.is_file():
            raise PlateImportError("Unable to read sliced plate data from this archive.")
        if path.stat().st_size != size:
            raise PlateImportError("Unable to read sliced plate data from this archive.")
        imported.append(SlicedPlate(number, filename, path))
    return imported


def extract_plates_into(archive_path, original_filename, destination) -> list:
    """Extract validated plates into an existing caller-owned directory."""
    return _run_isolated(archive_path, original_filename, destination)


@contextmanager
def extracted_plates(archive_path, original_filename):
    """Validate the whole archive before yielding bounded, locally named files."""
    with tempfile.TemporaryDirectory(prefix="odin-plates-") as tmp:
        yield extract_plates_into(archive_path, original_filename, tmp)
