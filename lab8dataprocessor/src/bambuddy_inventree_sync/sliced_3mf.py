from __future__ import annotations

from io import BytesIO
from pathlib import PurePosixPath
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


MAX_SLICE_INFO_BYTES = 5 * 1024 * 1024


def inspect_sliced_3mf(filename: str, content: bytes) -> dict[str, Any]:
    """Validate and summarize a Bambu Studio sliced ``.gcode.3mf`` archive."""
    result: dict[str, Any] = {
        "filename": PurePosixPath(filename).name,
        "valid": False,
        "plates": [],
    }
    if not filename.lower().endswith(".gcode.3mf"):
        result["error"] = "Потрібен нарізаний файл з розширенням .gcode.3mf"
        return result

    try:
        with ZipFile(BytesIO(content)) as archive:
            names = archive.namelist()
            names_by_lower = {name.lower(): name for name in names}
            gcode_names = [name for name in names if name.lower().endswith(".gcode")]
            slice_info_name = names_by_lower.get("metadata/slice_info.config")
            if not slice_info_name or not gcode_names:
                result["error"] = (
                    "Файл не містить G-code або Metadata/slice_info.config; "
                    "експортуйте його через Bambu Studio → Export plate sliced file"
                )
                return result

            info = archive.getinfo(slice_info_name)
            if info.file_size > MAX_SLICE_INFO_BYTES:
                result["error"] = "Metadata/slice_info.config перевищує допустимий розмір"
                return result
            root = ElementTree.fromstring(archive.read(slice_info_name))
    except (BadZipFile, ElementTree.ParseError, KeyError, OSError, ValueError) as exc:
        result["error"] = f"Не вдалося прочитати нарізаний .gcode.3mf: {exc}"
        return result

    normalized_gcodes = {name.lower() for name in gcode_names}
    plates: list[dict[str, Any]] = []
    for ordinal, element in enumerate(root.findall(".//plate"), start=1):
        metadata = {
            str(item.get("key") or ""): str(item.get("value") or "")
            for item in element.findall("metadata")
            if item.get("key")
        }
        plate_index = _as_int(metadata.get("index")) or ordinal
        declared_gcode = metadata.get("gcode_file")
        if declared_gcode:
            declared_normalized = declared_gcode.lstrip("/").lower()
            gcode_present = declared_normalized in normalized_gcodes
        elif len(gcode_names) == 1:
            declared_gcode = gcode_names[0]
            gcode_present = True
        else:
            expected = f"metadata/plate_{plate_index}.gcode"
            declared_gcode = expected
            gcode_present = expected in normalized_gcodes

        objects = [
            {
                "id": item.get("identify_id") or item.get("object_id"),
                "name": item.get("name"),
            }
            for item in element.findall("object")
            if str(item.get("skipped") or "false").lower() != "true"
        ]
        filaments = []
        for item in element.findall("filament"):
            filaments.append({
                "id": _as_int(item.get("id")),
                "tray_info_idx": item.get("tray_info_idx"),
                "type": item.get("type"),
                "color": item.get("color"),
                "used_g": _as_float(item.get("used_g")),
                "used_m": _as_float(item.get("used_m")),
                "used_for_object_g": _as_float(item.get("used_for_object")),
                "used_for_support_g": _as_float(item.get("used_for_support")),
            })

        warnings = [
            {
                "message": item.get("msg"),
                "level": _as_int(item.get("level")),
                "error_code": item.get("error_code"),
            }
            for item in element.findall("warning")
        ]
        used_g = sum(value["used_g"] or 0.0 for value in filaments)
        plate_weight = _as_float(metadata.get("weight"))
        plates.append({
            "index": plate_index,
            "gcode_file": declared_gcode,
            "gcode_present": gcode_present,
            "object_count": len(objects),
            "objects": objects,
            "print_time_seconds": _as_float(metadata.get("prediction")),
            "filament_weight_grams": plate_weight if plate_weight is not None else round(used_g, 3),
            "filaments": filaments,
            "printer_model_id": metadata.get("printer_model_id") or None,
            "nozzle_diameters": metadata.get("nozzle_diameters") or None,
            "support_used": _as_bool(metadata.get("support_used")),
            "toolpath_outside": _as_bool(metadata.get("outside")),
            "warnings": warnings,
        })

    printable_plates = [plate for plate in plates if plate["gcode_present"]]
    if not printable_plates:
        result["error"] = "У файлі не знайдено нарізаної пластини з G-code"
        result["plates"] = plates
        return result

    result.update({
        "valid": True,
        "plates": printable_plates,
        "plate_count": len(printable_plates),
        "gcode_files": gcode_names,
        "recommended_units_per_run": max(1, int(printable_plates[0]["object_count"] or 1)),
    })
    return result


def _as_float(value: Any) -> float | None:
    try:
        return float(str(value).strip()) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    return str(value).strip().lower() in {"1", "true", "yes"}
