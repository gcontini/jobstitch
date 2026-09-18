"""Reading multipart parts: same rules everywhere, checked before any work.

Every endpoint takes files, and most also accept the same thing as a plain
form field so ``curl -F jd_text=...`` works as well as ``-F jd=@JD.txt``. The
file wins when both are sent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import json

from fastapi import UploadFile

from .errors import BadPart, MissingPart, PartTooLarge

#: Strip a UTF-8 BOM: Windows editors add one and JSON parsers choke on it.
_BOM = "﻿"

#: How many images one request may carry. A template that needs more than a
#: handful is not what this is for, and each one costs a part-sized upload.
MAX_IMAGES = 10


async def bytes_part(
    upload: Optional[UploadFile], *, name: str, max_bytes: int, required: bool = False
) -> Optional[bytes]:
    """Read one uploaded file, refusing anything oversized before reading it."""
    if upload is None:
        if required:
            raise MissingPart(f"part '{name}' is required")
        return None
    size = getattr(upload, "size", None)
    if size is not None and size > max_bytes:
        raise PartTooLarge(f"part '{name}' is {size} bytes (limit {max_bytes})")
    blob = await upload.read()
    if len(blob) > max_bytes:
        raise PartTooLarge(f"part '{name}' is {len(blob)} bytes (limit {max_bytes})")
    if required and not blob:
        raise BadPart(f"part '{name}' is empty")
    return blob


async def text_part(
    upload: Optional[UploadFile],
    raw: Optional[str] = None,
    *,
    name: str,
    max_bytes: int,
    required: bool = False,
) -> Optional[str]:
    """A text part, from the file if one was sent, else from the form field."""
    blob = await bytes_part(upload, name=name, max_bytes=max_bytes, required=False)
    if blob is not None:
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BadPart(f"part '{name}' is not UTF-8 text: {exc}") from exc
    elif raw is not None:
        text = raw
    elif required:
        raise MissingPart(f"part '{name}' is required (as a file or a form field)")
    else:
        return None

    text = text.lstrip(_BOM)
    if required and not text.strip():
        raise BadPart(f"part '{name}' is empty")
    return text


async def json_part(
    upload: Optional[UploadFile],
    raw: Optional[str] = None,
    *,
    name: str,
    max_bytes: int,
    required: bool = False,
) -> Optional[Dict[str, Any]]:
    """A JSON object part, parsed and checked to be an object."""
    text = await text_part(upload, raw, name=name, max_bytes=max_bytes, required=required)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BadPart(f"part '{name}' is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise BadPart(f"part '{name}' must be a JSON object, got {type(data).__name__}")
    return data


async def images_part(
    uploads: Optional[List[UploadFile]], *, max_bytes: int
) -> Dict[str, bytes]:
    """The uploaded images, keyed by the name the template includes them under.

    The part's file name *is* the key — that is the whole interface — so it is
    reduced to a bare name here, before anything downstream sees a path.
    """
    uploads = uploads or []
    # Counted before anything is read: refusing fifty parts after reading them
    # is not a limit.
    if len(uploads) > MAX_IMAGES:
        raise BadPart(f"too many images: {len(uploads)} (limit {MAX_IMAGES})")
    images: Dict[str, bytes] = {}
    for upload in uploads:
        name = Path(upload.filename or "").name
        if not name or name.startswith("."):
            raise BadPart(f"image part has no usable file name: {upload.filename!r}")
        blob = await bytes_part(upload, name=f"images/{name}", max_bytes=max_bytes)
        if blob:
            images[name] = blob
    return images


__all__ = ["bytes_part", "text_part", "json_part", "images_part", "MAX_IMAGES"]
