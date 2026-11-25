"""Checked, streaming downloads using injected local or Kodi VFS operations."""

from dataclasses import dataclass
import uuid


_CHUNK_SIZE = 256 * 1024
_DEFAULT_PROGRESS_INTERVAL = 10


class DownloadError(RuntimeError):
    """A downloaded stream did not satisfy its declared size."""


@dataclass(frozen=True)
class DownloadReceipt:
    destination: str
    bytes_written: int


def _declared_length(response):
    value = None
    for name, header_value in response.headers.items():
        if name.lower() == "content-length":
            value = header_value
            break
    if value is None:
        return None
    try:
        length = int(value)
    except (TypeError, ValueError):
        return None
    return length if length >= 0 else None


def _progress_interval(progress):
    try:
        interval = int(getattr(progress, "interval", _DEFAULT_PROGRESS_INTERVAL))
    except (TypeError, ValueError):
        interval = _DEFAULT_PROGRESS_INTERVAL
    return max(1, interval)


def _remove_partial(file_ops, partial_path):
    if partial_path is None:
        return
    try:
        if file_ops.exists(partial_path):
            file_ops.remove(partial_path)
    except Exception:
        # Some VFS backends cannot remove files; never hide the transfer error.
        pass


def _write_all(output, chunk, display_name):
    offset = 0
    while offset < len(chunk):
        remaining = chunk[offset:]
        written = output.write(remaining)
        if (
            isinstance(written, bool)
            or not isinstance(written, int)
            or written <= 0
            or written > len(remaining)
        ):
            raise DownloadError(
                "Invalid download write progress for {0}: {1!r}".format(
                    display_name, written
                )
            )
        offset += written
    return offset


def stream_download(
    response,
    destination,
    display_name,
    file_ops,
    progress=None,
    chunk_size=_CHUNK_SIZE,
):
    """Stream a valid response to a unique sibling file, then atomically promote it.

    ``progress`` is an optional callable receiving ``(percent, display_name)``.
    ``chunk_size`` is forwarded to ``iter_content`` and defaults to 256 KiB.
    Its optional integer ``interval`` attribute controls percentage intervals and
    is clamped to at least one. A 100-percent update is emitted only after
    successful validation and atomic replacement; no Kodi notification is sent.
    """
    partial_path = None
    try:
        # Status validation must precede every filesystem side effect.
        response.raise_for_status()
        declared_length = _declared_length(response)
        interval = _progress_interval(progress) if progress is not None else 1
        partial_path = "{0}.part-{1}".format(destination, uuid.uuid4().hex)
        bytes_written = 0
        last_percent = None

        output = file_ops.open_write(partial_path)
        try:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                bytes_written += _write_all(output, chunk, display_name)
                if (
                    progress is not None
                    and declared_length is not None
                    and declared_length > 0
                ):
                    percent = min(99, (bytes_written * 100) // declared_length)
                    if percent % interval == 0 and percent != last_percent:
                        progress(percent, display_name)
                        last_percent = percent
        finally:
            output.close()

        if declared_length is not None and bytes_written != declared_length:
            raise DownloadError(
                "Download of {0} is incomplete: expected {1} bytes, received {2}".format(
                    display_name, declared_length, bytes_written
                )
            )

        file_ops.replace(partial_path, destination)
        partial_path = None
        if progress is not None and declared_length:
            progress(100, display_name)
        return DownloadReceipt(destination=destination, bytes_written=bytes_written)
    except Exception:
        _remove_partial(file_ops, partial_path)
        raise
    finally:
        response.close()
