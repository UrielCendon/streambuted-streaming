from dataclasses import dataclass

from app.errors import RangeNotSatisfiableError


@dataclass(frozen=True)
class ByteRange:
    """Resolved byte range for an object."""

    start: int
    end: int
    size_bytes: int

    @property
    def length(self) -> int:
        """Return the number of bytes included in the range."""
        return self.end - self.start + 1

    @property
    def content_range(self) -> str:
        """Return the HTTP Content-Range header value."""
        return f"bytes {self.start}-{self.end}/{self.size_bytes}"


def parse_range_header(range_header: str | None, size_bytes: int) -> ByteRange | None:
    """Parse a single HTTP bytes Range header.

    Args:
        range_header: Raw Range header value.
        size_bytes: Total object size.

    Returns:
        Parsed byte range or None when no Range header is present.

    Raises:
        RangeNotSatisfiableError: If the range is malformed or outside the object.
    """
    if range_header is None or not range_header.strip():
        return None
    if size_bytes <= 0:
        raise RangeNotSatisfiableError(size_bytes)

    normalized_header = range_header.strip()
    if not normalized_header.startswith("bytes="):
        raise RangeNotSatisfiableError(size_bytes)

    value = normalized_header.removeprefix("bytes=").strip()
    if "," in value or "-" not in value:
        raise RangeNotSatisfiableError(size_bytes)

    start_text, end_text = value.split("-", 1)
    if not start_text and not end_text:
        raise RangeNotSatisfiableError(size_bytes)

    try:
        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise ValueError
            start = max(size_bytes - suffix_length, 0)
            end = size_bytes - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size_bytes - 1
    except ValueError as exc:
        raise RangeNotSatisfiableError(size_bytes) from exc

    if start < 0 or end < start or start >= size_bytes:
        raise RangeNotSatisfiableError(size_bytes)

    return ByteRange(start=start, end=min(end, size_bytes - 1), size_bytes=size_bytes)
