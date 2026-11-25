"""Webshare Kodi presentation handlers with completion owned by ActionRunner."""

import io
import json
import ntpath
import os
import re
import tempfile

from unidecode import unidecode

try:
    from urllib.parse import urlencode
except ImportError:  # pragma: no cover - retained for older Kodi interpreters
    from urllib import urlencode

from yawsp_app.backup_db import BackupDBStore
from yawsp_app.download_jobs import (
    DownloadOperation,
    backup_db_job_key,
    media_job_key,
)
from yawsp_app.downloads import stream_download
from yawsp_app.metadata import apply_video_metadata
from yawsp_app.runtime import ActionResult, DirectoryResult, PlaybackResult, is_vfs_path
from yawsp_app.search import matches_filename, page_offsets, parse_query
from yawsp_app.webshare import WebshareError


_CATEGORIES = ("", "video", "images", "audio", "archives", "docs", "adult")
_SORTS = ("", "recent", "rating", "largest", "smallest")
_BROWSING_SENTINEL = "%#NONE#%"
_SEARCH_HISTORY = "search_history"
_BACKUP_DB = "D1iIcURxlR"
_BACKUP_DB_DOWNLOAD_CHUNK_SIZE = 256 * 1024


def _is_ok(xml):
    status = xml.find("status")
    if status is None or status.text is None or not status.text.strip():
        raise WebshareError("Webshare response is missing required <status>")
    return status.text == "OK"


def _to_dict(xml, skip=()):
    result = {}
    for element in xml:
        if element.tag in skip:
            continue
        value = (
            element.text
            if len(list(element)) == 0
            else _to_dict(element, skip)
        )
        if element.tag in result:
            if isinstance(result[element.tag], list):
                result[element.tag].append(value)
            else:
                result[element.tag] = [result[element.tag], value]
        else:
            result[element.tag] = value
    return result


_HISTORY_LIST = object()
_HISTORY_TUPLE = object()
_HISTORY_SET = object()
_HISTORY_FROZENSET = object()


def _history_content_key(value):
    """Recursively hash normal dictionary content without lossy field shortcuts."""
    if isinstance(value, dict):
        return frozenset(
            (_history_content_key(key), _history_content_key(item))
            for key, item in value.items()
        )
    if isinstance(value, list):
        return (_HISTORY_LIST, tuple(map(_history_content_key, value)))
    if isinstance(value, tuple):
        return (_HISTORY_TUPLE, tuple(map(_history_content_key, value)))
    if isinstance(value, set):
        return (_HISTORY_SET, frozenset(map(_history_content_key, value)))
    if isinstance(value, frozenset):
        return (_HISTORY_FROZENSET, frozenset(map(_history_content_key, value)))
    return value


def _deduplicate_history(files):
    """Return first occurrences using the former whole-dictionary equality."""
    files_by_content = set()
    unique = []
    for file_data in files:
        key = _history_content_key(file_data)
        if key not in files_by_content:
            files_by_content.add(key)
            unique.append(file_data)
    return unique


def _sizelize(value, units=None):
    units = units or ("B", "KB", "MB", "GB")
    if not value:
        return str(value or "?")
    try:
        size = float(value)
        for unit in units[:-1]:
            if size < 1024.0:
                return "{0:.0f}{1}".format(size, unit) if unit == "B" else "{0:.2f}{1}".format(size, unit)
            size /= 1024.0
        return "{0:.2f}{1}".format(size, units[-1])
    except (TypeError, ValueError):
        return str(value)


def _label(file_data):
    return "{0} ({1})".format(
        file_data["name"], _sizelize(file_data.get("size") or file_data.get("sizelized") or "?")
    )


def _url(runtime, params=None, action=None, **changes):
    values = dict(params or {})
    if action is not None:
        values["action"] = action
    for key, value in changes.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    return "{0}?{1}".format(runtime.base_url, urlencode(values))


def _listing_url(runtime, params, **changes):
    """Retain filters/paging while replacing transient listing operations."""
    state = dict(params)
    for transient in ("toqueue", "remove", "dequeue"):
        state.pop(transient, None)
    state.update(changes)
    return _url(runtime, state)


def _add_item(runtime, url, list_item, is_folder=False):
    runtime.add_item(url, list_item, is_folder)


def _valid_file_data(runtime, file_data):
    for field in ("name", "ident"):
        value = file_data.get(field)
        if not isinstance(value, str) or not value.strip():
            runtime.logger.error(
                "Skipping Webshare file row with missing or blank %s", field
            )
            return False
    return True


def _add_file(runtime, file_data, extra_commands=()):
    label = _label(file_data)
    item = runtime.list_item(label=label)
    if file_data.get("img"):
        item.setArt({"thumb": file_data["img"]})
    apply_video_metadata(item, {"title": label})
    item.setProperty("IsPlayable", "true")
    commands = [
        (
            runtime.localized(30211),
            "RunPlugin({0})".format(_url(runtime, action="info", ident=file_data["ident"])),
        ),
        (
            runtime.localized(30212),
            "RunPlugin({0})".format(_url(runtime, action="download", ident=file_data["ident"])),
        ),
    ]
    commands.extend(extra_commands)
    item.addContextMenuItems(commands)
    _add_item(
        runtime,
        _url(runtime, action="play", ident=file_data["ident"], name=file_data["name"]),
        item,
    )


def _infonize(data, key, process=str, showkey=True, prefix="", suffix="\n"):
    if key not in data:
        return ""
    value = process(data[key]) if callable(process) else str(data[key])
    label = key.capitalize() + ": " if showkey else ""
    return "{0}{1}{2}{3}".format(prefix, label, value, suffix)


def _fpsize(value):
    try:
        rounded = round(float(value), 3)
        return str(int(rounded)) if int(rounded) == rounded else str(rounded)
    except (TypeError, ValueError):
        return str(value)


def _info_text(data):
    text = ""
    text += _infonize(data, "name")
    text += _infonize(data, "size", _sizelize)
    for key in ("type", "width", "height", "format"):
        text += _infonize(data, key)
    text += _infonize(data, "fps", _fpsize)
    bitrate_size = lambda value: _sizelize(value, ("bps", "Kbps", "Mbps", "Gbps"))
    text += _infonize(data, "bitrate", bitrate_size)

    for media, title in (("video", "Video stream: "), ("audio", "Audio stream: ")):
        if media not in data or "stream" not in data[media]:
            continue
        streams = data[media]["stream"]
        streams = [streams] if isinstance(streams, dict) else streams
        for stream in streams:
            text += title
            if media == "video":
                text += _infonize(stream, "width", showkey=False, suffix="")
                text += _infonize(stream, "height", showkey=False, prefix="x", suffix="")
            text += _infonize(stream, "format", showkey=False, prefix=", ", suffix="")
            if media == "video":
                text += _infonize(stream, "fps", _fpsize, showkey=False, prefix=", ", suffix="")
            else:
                text += _infonize(stream, "channels", showkey=False, prefix=", ", suffix="")
            text += _infonize(stream, "bitrate", bitrate_size, showkey=False, prefix=", ", suffix="")
            text += "\n"
    text += _infonize(data, "removed", lambda value: "Yes" if value == "1" else "No")
    return text


def _join(folder, filename):
    if is_vfs_path(folder):
        return folder + filename if folder.endswith("/") else folder + "/" + filename
    return os.path.join(folder, filename)


def _safe_filename(value):
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value not in (".", "..")
        and "\x00" not in value
        and "/" not in value
        and "\\" not in value
        and not ntpath.splitdrive(value)[0]
        and not ntpath.isabs(value)
    )


def _download_name(candidates, normalize):
    for candidate in candidates:
        if normalize and isinstance(candidate, str):
            candidate = unidecode(candidate)
        if _safe_filename(candidate):
            return candidate
    return "download"


def _destination(folder, filename):
    destination = _join(folder, filename)
    if not is_vfs_path(folder):
        base = os.path.realpath(folder)
        target = os.path.realpath(destination)
        try:
            contained = os.path.commonpath((base, target)) == base
        except ValueError:
            contained = False
        if not contained:
            raise ValueError("Download destination escapes its configured folder")
    return destination


class _CancellableResponse:
    """Delegate a streaming response while observing job cancellation per chunk."""

    def __init__(self, response, context):
        self._response = response
        self._context = context

    @property
    def headers(self):
        return self._response.headers

    def raise_for_status(self):
        self._context.raise_if_cancelled()
        return self._response.raise_for_status()

    def iter_content(self, chunk_size=1, decode_unicode=False):
        self._context.raise_if_cancelled()
        for chunk in self._response.iter_content(chunk_size, decode_unicode):
            self._context.raise_if_cancelled()
            yield chunk
        self._context.raise_if_cancelled()

    def close(self):
        return self._response.close()


class _CancellableFileOps:
    """Prevent a cancelled transfer from replacing its final destination."""

    def __init__(self, file_ops, context):
        self._file_ops = file_ops
        self._context = context

    def open_write(self, path):
        self._context.raise_if_cancelled()
        return self._file_ops.open_write(path)

    def replace(self, source, destination):
        self._context.raise_if_cancelled()
        return self._file_ops.replace(source, destination)

    def remove(self, path):
        return self._file_ops.remove(path)

    def exists(self, path):
        return self._file_ops.exists(path)


class _MediaDownloadOperation(DownloadOperation):
    def __init__(
        self,
        worker_client_factory,
        ident,
        requested_name,
        folder,
        file_ops,
        normalize,
        notify_progress,
        interval,
        notify,
        logger,
        downloading_label,
        downloaded_label,
        failed_label,
    ):
        self.worker_client_factory = worker_client_factory
        self.ident = ident
        self.requested_name = requested_name
        self.folder = folder
        self.file_ops = file_ops
        self.normalize = normalize
        self.notify_progress = notify_progress
        self.interval = interval
        self.notify = notify
        self.logger = logger
        self.downloading_label = downloading_label
        self.downloaded_label = downloaded_label
        self.failed_label = failed_label
        self.display_name = _download_name((requested_name, ident), normalize)

    def run(self, context):
        context.raise_if_cancelled()
        client = self.worker_client_factory(context.session)
        token = client.revalidate()
        if not token:
            raise WebshareError("Webshare download requires authentication")
        link = client.get_link(self.ident, token, "file_download")
        if not link:
            raise WebshareError("Webshare download returned an empty link")
        xml = client.get_info(self.ident, token)
        metadata = _to_dict(xml)
        self.display_name = _download_name(
            (metadata.get("name"), self.requested_name, self.ident),
            self.normalize,
        )
        destination = _destination(self.folder, self.display_name)
        context.rekey(media_job_key(destination))
        context.raise_if_cancelled()

        if self.notify_progress:
            self.notify("{0} {1}".format(self.downloading_label, self.display_name))

        def progress(percent, name):
            context.raise_if_cancelled()
            self.notify("{0}% - {1}".format(percent, name))

        progress.interval = self.interval
        response = context.session.get(link, stream=True, timeout=60)
        stream_download(
            _CancellableResponse(response, context),
            destination,
            self.display_name,
            _CancellableFileOps(self.file_ops, context),
            progress if self.notify_progress else None,
        )
        return self.display_name

    def succeeded(self, display_name):
        self.notify("{0} {1}".format(self.downloaded_label, display_name))

    def failed(self, error):
        self.logger.error("Webshare download failed: %s", error)
        self.notify("{0} {1}".format(self.failed_label, self.display_name))


class _BackupDatabaseOperation(DownloadOperation):
    def __init__(
        self,
        backup_store,
        worker_client_factory,
        notify,
        refresh,
        logger,
        installed_label,
        failed_label,
        queue_ident=None,
        queue_succeeded_label=None,
        queue_failed_label=None,
    ):
        self.backup_store = backup_store
        self.worker_client_factory = worker_client_factory
        self.notify = notify
        self.refresh = refresh
        self.logger = logger
        self.installed_label = installed_label
        self.failed_label = failed_label
        self.queue_ident = queue_ident
        self.queue_succeeded_label = queue_succeeded_label
        self.queue_failed_label = queue_failed_label

    def _load_archive(self, context, client, token):
        archive_path = None
        try:
            context.raise_if_cancelled()
            link = client.get_link(_BACKUP_DB, token)
            response = context.session.get(link, stream=True, timeout=60)
            try:
                response.raise_for_status()
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=".backup-db-download-",
                    suffix=".zip",
                    dir=self.backup_store.profile,
                    delete=False,
                ) as archive_output:
                    archive_path = archive_output.name
                    for chunk in response.iter_content(
                        chunk_size=_BACKUP_DB_DOWNLOAD_CHUNK_SIZE
                    ):
                        context.raise_if_cancelled()
                        if chunk:
                            archive_output.write(chunk)
                    context.raise_if_cancelled()
            finally:
                response.close()
            return archive_path
        except Exception:
            if archive_path is not None:
                try:
                    os.remove(archive_path)
                except OSError:
                    pass
            raise

    def run(self, context):
        client = self.worker_client_factory(context.session)
        token = client.revalidate()
        if not token:
            raise WebshareError("Backup DB installation requires authentication")
        context.raise_if_cancelled()
        self.backup_store.ensure_installed(
            lambda: self._load_archive(context, client, token)
        )
        if self.queue_ident:
            context.raise_if_cancelled()
            try:
                xml = client._post(
                    "queue_file", {"ident": self.queue_ident, "wst": token}
                )
                queue_message = (
                    self.queue_succeeded_label
                    if _is_ok(xml)
                    else self.queue_failed_label
                )
            except Exception as error:
                self.logger.error("Backup DB queue action failed: %s", error)
                queue_message = self.queue_failed_label
            context.raise_if_cancelled()
            try:
                self.notify(queue_message)
            except Exception:
                self.logger.exception("Backup DB queue notification failed")
        context.raise_if_cancelled()
        return True

    def succeeded(self, installed):
        try:
            self.refresh()
        except Exception:
            self.logger.exception("Backup DB completion refresh failed")
        try:
            self.notify(self.installed_label)
        except Exception:
            self.logger.exception("Backup DB installed notification failed")

    def failed(self, error):
        self.logger.error("Backup DB installation failed: %s", error)
        self.notify(self.failed_label)


def build_webshare_handlers(
    runtime,
    client_factory,
    download_jobs=None,
    worker_client_factory=None,
    notify=None,
    refresh=None,
):
    """Build Webshare handlers; none calls Kodi directory/playback completion."""
    client = client_factory()
    backup_store = BackupDBStore(runtime.profile)
    history_path = os.path.join(runtime.profile, _SEARCH_HISTORY)
    worker_client_factory = worker_client_factory or (lambda session: client_factory())
    notify = notify or runtime.notify
    refresh = refresh or (lambda: runtime.execute_builtin("Container.Refresh"))

    def token_or_notify():
        token = client.revalidate()
        if not token:
            runtime.notify(runtime.localized(30101) or runtime.localized(30102))
            return None
        return token

    def load_search_history():
        try:
            if not os.path.exists(history_path):
                return []
            with io.open(history_path, "r", encoding="utf-8") as source:
                history = json.load(source)
            return history if isinstance(history, list) else []
        except (OSError, UnicodeError, ValueError):
            runtime.logger.exception("Cannot load Webshare search history")
            return []

    def save_search_history(history):
        try:
            os.makedirs(runtime.profile, exist_ok=True)
            with io.open(history_path, "w", encoding="utf-8") as output:
                json.dump(history, output)
        except (OSError, TypeError, ValueError):
            runtime.logger.exception("Cannot save Webshare search history")

    def store_search(what):
        if not what:
            return
        history = load_search_history()
        if what in history:
            history.remove(what)
        history.insert(0, what)
        save_search_history(history[: int(runtime.get_setting("shistory") or 20)])

    def remove_search(what):
        if not what:
            return
        history = load_search_history()
        if what in history:
            history.remove(what)
            save_search_history(history)

    def notify_api_result(xml, success_message):
        if _is_ok(xml):
            if success_message:
                runtime.notify(success_message)
            return True
        runtime.notify(runtime.localized(30107))
        return False

    def to_queue(ident, token):
        return notify_api_result(
            client._post("queue_file", {"ident": ident, "wst": token}),
            runtime.localized(30105),
        )

    def search_menu():
        item = runtime.list_item(label=runtime.localized(30205))
        item.setArt({"icon": "DefaultAddSource.png"})
        _add_item(runtime, _url(runtime, action="search", what=""), item, True)

        for label_id, sort in ((30208, _SORTS[1]), (30209, _SORTS[3])):
            item = runtime.list_item(label=runtime.localized(label_id))
            item.setArt({"icon": "DefaultAddonsRecentlyUpdated.png" if sort == "recent" else "DefaultHardDisk.png"})
            _add_item(
                runtime,
                _url(runtime, action="search", what=_BROWSING_SENTINEL, sort=sort),
                item,
                True,
            )

        for search_term in load_search_history():
            item = runtime.list_item(label=search_term)
            item.setArt({"icon": "DefaultAddonsSearch.png"})
            item.addContextMenuItems(
                [
                    (
                        runtime.localized(30213),
                        "Container.Update({0})".format(
                            _url(runtime, action="search", remove=search_term)
                        ),
                    )
                ]
            )
            _add_item(
                runtime,
                _url(runtime, action="search", what=search_term, ask=1),
                item,
                True,
            )

    def present_search(params, token, what, category, sort, limit, offset):
        query = parse_query(what)
        page = client.search(token, query, category, sort, limit, offset)
        previous, following = page_offsets(offset, limit, page.raw_count)
        if previous is not None:
            item = runtime.list_item(label=runtime.localized(30206))
            item.setArt({"icon": "DefaultAddonsSearch.png"})
            _add_item(
                runtime,
                _listing_url(runtime, params, offset=previous),
                item,
                True,
            )

        for element in page.files:
            file_data = _to_dict(element)
            if not matches_filename(query, file_data["name"]):
                continue
            queue_url = _listing_url(runtime, params, toqueue=file_data["ident"])
            commands = [
                (
                    runtime.localized(30214),
                    "Container.Update({0})".format(queue_url),
                )
            ]
            _add_file(runtime, file_data, commands)

        if following is not None:
            item = runtime.list_item(label=runtime.localized(30207))
            item.setArt({"icon": "DefaultAddonsSearch.png"})
            _add_item(
                runtime,
                _listing_url(runtime, params, offset=following),
                item,
                True,
            )

    def search(params):
        runtime.set_category(
            "{0} / {1}".format(runtime.addon_info("name"), runtime.localized(30201))
        )
        token = None

        def lazy_token():
            nonlocal token
            if token is None:
                token = token_or_notify()
            return token

        update_listing = False
        if "remove" in params:
            remove_search(params["remove"])
            update_listing = True
        if "toqueue" in params:
            token = lazy_token()
            if not token:
                return DirectoryResult(succeeded=False)
            to_queue(params["toqueue"], token)
            update_listing = True

        what = params.get("what")
        if what is None and "ask" not in params:
            what = runtime.ask("", runtime.localized(30007))
            if what is None:
                update_listing = True
            else:
                store_search(what)

        if what is not None:
            runtime.set_setting(
                "slast", _BROWSING_SENTINEL if "offset" in params else what
            )
            if "offset" in params:
                update_listing = True
            category = params.get(
                "category", _CATEGORIES[int(runtime.get_setting("scategory") or 0)]
            )
            sort = params.get("sort", _SORTS[int(runtime.get_setting("ssort") or 0)])
            limit = int(params.get("limit", 500))
            offset = int(params.get("offset", 0))
            token = lazy_token()
            if not token:
                return DirectoryResult(succeeded=False)
            present_search(params, token, what, category, sort, limit, offset)
        else:
            runtime.set_setting("slast", _BROWSING_SENTINEL)
            search_menu()
        return DirectoryResult(update_listing=update_listing)

    def queue(params):
        runtime.set_category(runtime.localized(30202))
        token = token_or_notify()
        if not token:
            return DirectoryResult(succeeded=False)
        update_listing = False
        if "dequeue" in params:
            notify_api_result(
                client._post("dequeue_file", {"ident": params["dequeue"], "wst": token}),
                runtime.localized(30106),
            )
            update_listing = True

        xml = client._post("queue", {"wst": token})
        if notify_api_result(xml, None):
            for element in xml.iter("file"):
                file_data = _to_dict(element)
                if not _valid_file_data(runtime, file_data):
                    continue
                commands = [
                    (
                        runtime.localized(30215),
                        "Container.Update({0})".format(
                            _url(runtime, action="queue", dequeue=file_data["ident"])
                        ),
                    )
                ]
                _add_file(runtime, file_data, commands)
        return DirectoryResult(update_listing=update_listing)

    def history(params):
        runtime.set_category(runtime.localized(30203))
        token = token_or_notify()
        if not token:
            return DirectoryResult(succeeded=False)
        update_listing = False
        xml = None
        if "remove" in params:
            xml = client._post("history", {"wst": token})
            ids = []
            if notify_api_result(xml, None):
                for element in xml.iter("file"):
                    download_id = (element.findtext("download_id") or "").strip()
                    if element.findtext("ident") == params["remove"] and download_id:
                        ids.append(download_id)
            if ids:
                notify_api_result(
                    client._post("clear_history", {"ids[]": ids, "wst": token}),
                    runtime.localized(30104),
                )
                xml = None
            update_listing = True
        if "toqueue" in params:
            to_queue(params["toqueue"], token)
            update_listing = True

        if xml is None:
            xml = client._post("history", {"wst": token})
        if notify_api_result(xml, None):
            files = (
                _to_dict(element, ("ended_at", "download_id", "started_at"))
                for element in xml.iter("file")
            )
            files = _deduplicate_history(
                file_data
                for file_data in files
                if _valid_file_data(runtime, file_data)
            )
            for file_data in files:
                commands = [
                    (
                        runtime.localized(30213),
                        "Container.Update({0})".format(
                            _url(runtime, action="history", remove=file_data["ident"])
                        ),
                    ),
                    (
                        runtime.localized(30214),
                        "Container.Update({0})".format(
                            _url(runtime, action="history", toqueue=file_data["ident"])
                        ),
                    ),
                ]
                _add_file(runtime, file_data, commands)
        return DirectoryResult(update_listing=update_listing)

    def settings(params):
        runtime.open_settings()
        return ActionResult()

    def info(params):
        token = token_or_notify()
        if not token:
            return ActionResult(handled=False)
        xml = client.get_info(params["ident"], token)
        runtime.show_text(runtime.addon_info("name"), _info_text(_to_dict(xml)))
        return ActionResult()

    def play(params):
        token = token_or_notify()
        if not token:
            return PlaybackResult(False, runtime.list_item(label=params.get("name", "")))
        link = client.get_link(params["ident"], token)
        if not link:
            return PlaybackResult(False, runtime.list_item(label=params.get("name", "")))
        headers = dict(getattr(runtime.session, "headers", {}))
        headers.update({"Cookie": "wst={0}".format(token)})
        path = "{0}|{1}".format(link, urlencode(headers))
        item = runtime.list_item(label=params.get("name", ""), path=path)
        item.setProperty("mimetype", "application/octet-stream")
        return PlaybackResult(True, item)

    def download(params):
        where = runtime.get_setting("dfolder")
        file_ops = runtime.file_ops_for(where) if where else runtime.file_ops
        if not where or not file_ops.exists(where):
            runtime.notify("Set download folder!")
            runtime.open_settings()
            return ActionResult(handled=False)

        normalize = runtime.get_setting("dnormalize") == "true"
        notify_progress = runtime.get_setting("dnotify") == "true"
        try:
            interval = int(re.sub(r"[^\d]+", "", runtime.get_setting("dnevery") or "10"))
        except ValueError:
            interval = 10
        display_name = _download_name(
            (params.get("name"), params.get("ident")), normalize
        )
        destination = _destination(where, display_name)
        operation = _MediaDownloadOperation(
            worker_client_factory=worker_client_factory,
            ident=params["ident"],
            requested_name=params.get("name"),
            folder=where,
            file_ops=file_ops,
            normalize=normalize,
            notify_progress=notify_progress,
            interval=interval,
            notify=notify,
            logger=runtime.logger,
            downloading_label=runtime.localized(30302),
            downloaded_label=runtime.localized(30303),
            failed_label=runtime.localized(30304),
        )
        if download_jobs is None or not download_jobs.submit(
            media_job_key(destination), operation
        ):
            runtime.notify(
                "{0} {1}".format(runtime.localized(30437), display_name)
            )
            return ActionResult(handled=False)
        if notify_progress:
            runtime.notify("{0} {1}".format(runtime.localized(30433), display_name))
        return ActionResult()

    def database_status_item():
        item = runtime.list_item(label=runtime.localized(30434))
        item.setArt({"icon": "DefaultAddonsZip.png"})
        _add_item(runtime, _url(runtime, action="db"), item)

    def ensure_database_available(params):
        if backup_store.is_valid():
            return True
        key = backup_db_job_key(runtime.profile)
        status = download_jobs.status(key) if download_jobs is not None else None
        if status in ("queued", "running"):
            database_status_item()
            return False
        if status == "succeeded" and backup_store.is_valid():
            return True
        operation = _BackupDatabaseOperation(
            backup_store=backup_store,
            worker_client_factory=worker_client_factory,
            notify=notify,
            refresh=refresh,
            logger=runtime.logger,
            installed_label=runtime.localized(30435),
            failed_label=runtime.localized(30436),
            queue_ident=params.get("toqueue"),
            queue_succeeded_label=runtime.localized(30105),
            queue_failed_label=runtime.localized(30107),
        )
        accepted = download_jobs is not None and download_jobs.submit(key, operation)
        if accepted or (
            download_jobs is not None
            and download_jobs.status(key) in ("queued", "running")
        ):
            database_status_item()
            return False
        runtime.notify(runtime.localized(30436))
        raise WebshareError("Backup DB installation could not be queued")

    def database_listing(params, token=None):
        if not ensure_database_available(params):
            return DirectoryResult()
        update_listing = False
        if "toqueue" in params:
            if token is None:
                token = token_or_notify()
            if not token:
                return DirectoryResult(succeeded=False)
            to_queue(params["toqueue"], token)
            update_listing = True

        search_query = params.get("search", "").casefold()
        if "file" in params and "key" in params:
            data = backup_store.load(params["file"])
            selected = next((item for item in data if item.get("id") == params["key"]), None)
            if selected is not None:
                for stream in selected.get("streams", []):
                    file_data = {
                        "ident": stream["ident"],
                        "name": "{0} - {1}{2}".format(
                            stream["quality"], stream["lang"], stream.get("ainfo", "")
                        ),
                        "sizelized": stream["size"],
                    }
                    queue_url = _listing_url(
                        runtime,
                        params,
                        action="db",
                        toqueue=stream["ident"],
                    )
                    commands = [
                        (
                            runtime.localized(30214),
                            "Container.Update({0})".format(queue_url),
                        )
                    ]
                    item = runtime.list_item(label=_label(file_data))
                    apply_video_metadata(item, {"title": selected["title"]})
                    item.setProperty("IsPlayable", "true")
                    item.addContextMenuItems(commands)
                    _add_item(
                        runtime,
                        _url(
                            runtime,
                            action="play",
                            ident=stream["ident"],
                            name=selected["title"],
                        ),
                        item,
                    )
            runtime.sort_by_label()
        elif "file" in params:
            for data_item in backup_store.load(params["file"]):
                title = data_item.get("title", "")
                if search_query and search_query not in title.casefold():
                    continue
                item = runtime.list_item(label=title)
                if "plot" in data_item:
                    apply_video_metadata(item, {"title": title, "plot": data_item["plot"]})
                _add_item(
                    runtime,
                    _url(
                        runtime,
                        action="db",
                        file=params["file"],
                        key=data_item["id"],
                    ),
                    item,
                    True,
                )
            runtime.sort_by_label()
        else:
            if not search_query:
                item = runtime.list_item(label=runtime.localized(30010))
                item.setArt({"icon": "DefaultAddonsSearch.png"})
                _add_item(runtime, _url(runtime, action="searchdb"), item, True)
            if os.path.isdir(backup_store.dbdir):
                for filename in sorted(os.listdir(backup_store.dbdir)):
                    if not filename.lower().endswith(".txt"):
                        continue
                    if not os.path.isfile(os.path.join(backup_store.dbdir, filename)):
                        continue
                    item = runtime.list_item(label=os.path.splitext(filename)[0])
                    _add_item(
                        runtime,
                        _url(runtime, action="db", file=filename),
                        item,
                        True,
                    )
        return DirectoryResult(update_listing=update_listing)

    def db(params):
        return database_listing(params)

    def searchdb(params):
        query = params.get("query")
        if query is None:
            query = runtime.ask("", runtime.localized(30216))
        if query is not None:
            query = query.strip()
        if not query:
            return DirectoryResult()
        return database_listing(
            {
                "search": query,
                "file": backup_store.group_for_query(query),
            }
        )

    def menu(params):
        runtime.set_category(runtime.addon_info("name"))
        entries = (
            (runtime.localized(30201), "search", "DefaultAddonsSearch.png"),
            (runtime.localized(30202), "queue", "DefaultPlaylist.png"),
            (runtime.localized(30203), "history", "DefaultAddonsUpdates.png"),
            ("Backup DB", "db", "DefaultAddonsZip.png"),
        )
        for label, action, icon in entries:
            item = runtime.list_item(label=label)
            item.setArt({"icon": icon})
            _add_item(runtime, _url(runtime, action=action), item, True)
        item = runtime.list_item(label=runtime.localized(30204))
        item.setArt({"icon": "DefaultAddonService.png"})
        _add_item(runtime, _url(runtime, action="settings"), item)
        return DirectoryResult()

    return {
        "menu": menu,
        "search": search,
        "queue": queue,
        "history": history,
        "settings": settings,
        "info": info,
        "play": play,
        "download": download,
        "db": db,
        "searchdb": searchdb,
    }
