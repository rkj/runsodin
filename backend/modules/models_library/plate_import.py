"""Bounded extraction of sliced Orca/Bambu 3MF plates; never executes G-code."""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import tempfile
import stat
import zipfile

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_PLATE_BYTES = 100 * 1024 * 1024
MAX_PLATES = 100
MAX_MEMBERS = 10000
_PLATE = re.compile(r"Metadata/plate_([0-9]+)\.gcode", re.IGNORECASE)


class PlateImportError(ValueError):
    pass


@dataclass(frozen=True)
class SlicedPlate:
    number: int
    filename: str
    path: Path


@contextmanager
def extracted_plates(archive_path, original_filename):
    """Validate the whole archive before yielding bounded, locally named files.

    Only member data is read, never ZipFile.extract/extractall. Generated paths
    are unrelated to zip member paths, and remain owned by this context manager.
    """
    if Path(archive_path).stat().st_size > MAX_ARCHIVE_BYTES:
        raise PlateImportError("File too large. Maximum upload size is 100 MB.")
    try:
        zf = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as exc:
        raise PlateImportError("Invalid .3mf file (bad zip structure).") from exc
    with zf, tempfile.TemporaryDirectory(prefix="odin-plates-") as tmp:
        entries = zf.infolist()
        if len(entries) > MAX_MEMBERS:
            raise PlateImportError("Archive contains too many entries.")
        if sum(item.file_size for item in entries) > MAX_TOTAL_BYTES:
            raise PlateImportError("File rejected: decompressed size exceeds 500 MB limit.")
        seen_names, seen_plates, members = set(), set(), []
        for item in entries:
            name = item.filename
            parts = PurePosixPath(name).parts
            if ("\\" in name or name.startswith("/") or ".." in parts
                    or any(":" in part for part in parts)):
                raise PlateImportError("Archive contains an unsafe member path.")
            canonical = str(PurePosixPath(name))
            if name.rstrip("/") != canonical:
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
        # Original filenames are presentation only; strip both separator forms.
        original = str(original_filename or "project.3mf").replace("\\", "/").rsplit("/", 1)[-1]
        stem = re.sub(r"[^a-zA-Z0-9._-]", "_", Path(original).stem)[:150] or "project"
        plates = []
        for number, member in sorted(members, key=lambda pair: pair[0]):
            filename = f"{stem}_plate_{number}.gcode"
            path = Path(tmp) / filename
            size = 0
            try:
                with zf.open(member) as source, path.open("xb") as target:
                    while chunk := source.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_PLATE_BYTES or size > member.file_size:
                            raise PlateImportError("Plate decompression exceeded its declared size.")
                        target.write(chunk)
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
                raise PlateImportError("Unable to read sliced plate data from this archive.") from exc
            if size != member.file_size:
                raise PlateImportError("Incomplete sliced plate data.")
            plates.append(SlicedPlate(number, filename, path))
        yield plates
