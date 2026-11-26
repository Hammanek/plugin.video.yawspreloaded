"""Closed typed descriptors and bounded result codecs for the TMDB service."""

import copy
import hashlib
import json
import math
import re

from yawsp_app.tmdb_youtube import valid_youtube_id


SCHEMA_VERSION = 1
MAX_JOB_CANONICAL_BYTES = 262144
MAX_RESULT_CANONICAL_BYTES = 1048576
_KEY_PREFIX = b"tmdb-service-v1\x00"
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_SESSION_BINDING = re.compile(r"^v1:[0-9a-f]{64}$")
_CATALOG_SOURCES = frozenset(
    ("popular_movies", "popular_shows", "top_rated_movies", "top_rated_shows")
)
_OUTCOMES = frozenset(("succeeded", "failed", "aborted"))
_ERROR_MESSAGES = {
    "FAILED": "TMDB operation failed",
    "ABORTED": "TMDB operation aborted",
    "UNAVAILABLE": "TMDB service unavailable",
    "BACKPRESSURED": "TMDB service backpressured",
    "SHUTTING_DOWN": "TMDB service shutting down",
    "RESULT_EXPIRED": "TMDB service result expired",
}


class ProtocolError(ValueError):
    """A descriptor or result is outside the closed TMDB service protocol."""


def _reject_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolError("duplicate JSON object field")
        value[key] = item
    return value


def _reject_constant(_value):
    raise ProtocolError("nonfinite JSON number")


def _check_json_value(value):
    if value is None or type(value) is bool or type(value) is str:
        return
    if type(value) is int:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ProtocolError("nonfinite JSON number")
        return
    if type(value) is list:
        for item in value:
            _check_json_value(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ProtocolError("JSON object keys must be strings")
            _check_json_value(item)
        return
    raise ProtocolError("value is not detached JSON data")


def _canonical(document, limit):
    try:
        _check_json_value(document)
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except ProtocolError:
        raise
    except (
        TypeError,
        ValueError,
        OverflowError,
        UnicodeError,
        RecursionError,
    ) as error:
        raise ProtocolError("document is not valid bounded JSON") from error
    if len(encoded) > limit:
        raise ProtocolError("canonical document exceeds its byte bound")
    return encoded


def _decoded(document, limit):
    if isinstance(document, (str, bytes)):
        try:
            raw = document.encode("utf-8") if isinstance(document, str) else document
        except UnicodeError as error:
            raise ProtocolError("document is not valid scalar text") from error
        if len(raw) > limit:
            raise ProtocolError("document exceeds its byte bound")
        try:
            text = raw.decode("utf-8")
            document = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except ProtocolError:
            raise
        except (UnicodeError, ValueError, TypeError, RecursionError) as error:
            raise ProtocolError("malformed JSON document") from error
    encoded = _canonical(document, limit)
    try:
        detached = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise ProtocolError("document could not be detached") from error
    return detached, encoded


def _text(value, name, strip=False, nonempty=True):
    if strip:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ProtocolError(name + " must be text")
        if isinstance(value, float) and not math.isfinite(value):
            raise ProtocolError(name + " numeric value must be finite")
        value = str(value).strip()
    if type(value) is not str:
        raise ProtocolError(name + " must be text")
    if nonempty and not value:
        raise ProtocolError(name + " must not be empty")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise ProtocolError(name + " must be valid scalar text") from error
    return value


def _identifier(value, name="media_id", normalize=False):
    if normalize and isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if type(value) is not int or value <= 0:
        raise ProtocolError(name + " must be a positive integer")
    return value


def _nonnegative(value, name):
    if type(value) is not int or value < 0:
        raise ProtocolError(name + " must be a non-negative integer")
    return value


def _positive(value, name):
    if type(value) is not int or value < 1:
        raise ProtocolError(name + " must be a positive integer")
    return value


def _boolean(value, name):
    if type(value) is not bool:
        raise ProtocolError(name + " must be boolean")
    return value


def _binding(value):
    value = _text(value, "session_binding")
    if _SESSION_BINDING.fullmatch(value) is None:
        raise ProtocolError("session_binding must be a versioned SHA-256 digest")
    return value


def _scope(value, allow_empty_account=False):
    if type(value) is dict:
        if set(value) != {"account_id", "media", "language", "schema"}:
            raise ProtocolError("scope has unknown or missing fields")
        account_id = value["account_id"]
        media = value["media"]
        language = value["language"]
        schema = value["schema"]
    else:
        try:
            account_id = value.account_id
            media = value.media
            language = value.language
            schema = value.schema
        except (AttributeError, TypeError) as error:
            raise ProtocolError("invalid snapshot scope") from error
    account_id = _text(
        account_id,
        "scope account_id",
        strip=True,
        nonempty=not allow_empty_account,
    )
    language = _text(language, "scope language", strip=True)
    if media not in ("movies", "tv") or type(media) is not str:
        raise ProtocolError("scope media must be movies or tv")
    if type(schema) is not int or schema != SCHEMA_VERSION:
        raise ProtocolError("unsupported snapshot scope schema")
    return {
        "account_id": account_id,
        "media": media,
        "language": language,
        "schema": schema,
    }


def _media(value):
    value = _text(value, "media", strip=True)
    if value not in ("movie", "tv"):
        raise ProtocolError("media must be movie or tv")
    return value


def _language(value):
    return _text(value, "language", strip=True)


def _source(value):
    value = _text(value, "source", strip=True)
    if value not in _CATALOG_SOURCES:
        raise ProtocolError("unknown discovery source")
    return value


def _job_id(value):
    if type(value) is not str or _JOB_ID.fullmatch(value) is None:
        raise ProtocolError("job_id must be 32 lowercase hexadecimal characters")
    return value


def _exact_fields(document, expected):
    if type(document) is not dict or set(document) != set(expected):
        raise ProtocolError("document has unknown or missing fields")


def _validated_scope(value, allow_empty_account=False):
    scope = _scope(value, allow_empty_account=allow_empty_account)
    if scope != value or type(value) is not dict:
        raise ProtocolError("scope document is not canonical")
    return scope


def trailer_request(media, media_id):
    return {
        "operation": "metadata.trailer",
        "params": {
            "media": _media(media),
            "media_id": _identifier(media_id, normalize=True),
        },
    }


def show_request(show_id):
    return {
        "operation": "metadata.show",
        "params": {"show_id": _identifier(show_id, "show_id", normalize=True)},
    }


def watchlist_request(
    scope, generation, force, allow_missing_account, session_binding
):
    return {
        "operation": "watchlist.refresh",
        "params": {
            "scope": _scope(
                scope, allow_empty_account=allow_missing_account is True
            )
        },
        "generation": _nonnegative(generation, "generation"),
        "force": _boolean(force, "force"),
        "allow_missing_account": _boolean(
            allow_missing_account, "allow_missing_account"
        ),
        "session_binding": _binding(session_binding),
    }


def watched_request(scope, media_id, session_binding):
    return {
        "operation": "watchlist.watched",
        "params": {
            "scope": _scope(scope, allow_empty_account=True),
            "media_id": _identifier(media_id, normalize=True),
        },
        "session_binding": _binding(session_binding),
    }


def catalog_request(source, language):
    return {
        "operation": "discovery.catalog",
        "params": {"source": _source(source), "language": _language(language)},
    }


def seasons_request(show_id, language, session_binding):
    return {
        "operation": "details.seasons",
        "params": {
            "show_id": _identifier(show_id, "show_id", normalize=True),
            "language": _language(language),
        },
        "session_binding": _binding(session_binding),
    }


def episodes_request(show_id, season, language, session_binding):
    if isinstance(season, str) and season.strip().isdigit():
        season = int(season.strip())
    return {
        "operation": "details.episodes",
        "params": {
            "show_id": _identifier(show_id, "show_id", normalize=True),
            "season": _nonnegative(season, "season"),
            "language": _language(language),
        },
        "session_binding": _binding(session_binding),
    }


def invalidate_identity_request(account_id, session_binding):
    return {
        "operation": "identity.invalidate",
        "params": {"account_id": _text(account_id, "account_id", strip=True)},
        "session_binding": _binding(session_binding),
    }


def _validate_params(operation, params):
    if operation == "metadata.trailer":
        _exact_fields(params, ("media", "media_id"))
        return {
            "media": _media(params["media"]),
            "media_id": _identifier(params["media_id"]),
        }
    if operation == "metadata.show":
        _exact_fields(params, ("show_id",))
        return {"show_id": _identifier(params["show_id"], "show_id")}
    if operation == "watchlist.refresh":
        _exact_fields(params, ("scope",))
        return {"scope": _validated_scope(params["scope"], allow_empty_account=True)}
    if operation == "watchlist.watched":
        _exact_fields(params, ("scope", "media_id"))
        return {
            "scope": _validated_scope(params["scope"], allow_empty_account=True),
            "media_id": _identifier(params["media_id"]),
        }
    if operation == "discovery.catalog":
        _exact_fields(params, ("source", "language"))
        source = params["source"]
        if type(source) is not str:
            raise ProtocolError("unknown discovery source")
        return {"source": _source(source), "language": _language(params["language"])}
    if operation in ("details.seasons", "details.episodes"):
        fields = ("show_id", "language")
        if operation == "details.episodes":
            fields = ("show_id", "season", "language")
        _exact_fields(params, fields)
        result = {
            "show_id": _identifier(params["show_id"], "show_id"),
            "language": _language(params["language"]),
        }
        if operation == "details.episodes":
            result["season"] = _nonnegative(params["season"], "season")
        return result
    if operation == "identity.invalidate":
        _exact_fields(params, ("account_id",))
        return {
            "account_id": _text(params["account_id"], "account_id", strip=True)
        }
    raise ProtocolError("unknown TMDB service operation")


def _validate_job_fields(document):
    operation = document["operation"]
    if type(operation) is not str:
        raise ProtocolError("operation must be a named string")
    fields = {
        "metadata.trailer": ("operation", "params"),
        "metadata.show": ("operation", "params"),
        "watchlist.refresh": (
            "operation",
            "params",
            "generation",
            "force",
            "allow_missing_account",
            "session_binding",
        ),
        "watchlist.watched": ("operation", "params", "session_binding"),
        "discovery.catalog": ("operation", "params"),
        "details.seasons": ("operation", "params", "session_binding"),
        "details.episodes": ("operation", "params", "session_binding"),
        "identity.invalidate": ("operation", "params", "session_binding"),
    }.get(operation)
    if fields is None:
        raise ProtocolError("unknown TMDB service operation")
    _exact_fields(document, fields)
    if operation == "watchlist.refresh":
        _nonnegative(document["generation"], "generation")
        _boolean(document["force"], "force")
        _boolean(document["allow_missing_account"], "allow_missing_account")
    if "session_binding" in document:
        _binding(document["session_binding"])
    params = document["params"]
    if (
        operation == "watchlist.refresh"
        and not document["allow_missing_account"]
        and not params.get("scope", {}).get("account_id")
    ):
        raise ProtocolError("scope account_id must not be empty")
    if _validate_params(operation, params) != params:
        raise ProtocolError("request parameters are not builder-canonical")


def validate_job(document):
    detached, _encoded = _decoded(document, MAX_JOB_CANONICAL_BYTES)
    try:
        _validate_job_fields(detached)
    except ProtocolError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise ProtocolError("invalid TMDB service request") from error
    return detached


def dedup_key(request):
    detached = validate_job(request)
    encoded = _canonical(detached, MAX_JOB_CANONICAL_BYTES)
    return hashlib.sha256(_KEY_PREFIX + encoded).hexdigest()


def _validate_result_value(value):
    if type(value) is not dict:
        return
    keys = set(value)
    if "status" in keys or "youtube_id" in keys:
        if keys != {"status", "youtube_id"}:
            raise ProtocolError("trailer result has unknown or missing fields")
        if value["status"] not in ("hit", "missing", "error"):
            raise ProtocolError("unknown trailer result status")
        youtube_id = value["youtube_id"]
        if value["status"] == "hit":
            if not valid_youtube_id(youtube_id):
                raise ProtocolError("trailer hit requires a YouTube ID")
        elif youtube_id is not None:
            raise ProtocolError(
                "missing/error trailer result cannot have a YouTube ID"
            )
    elif "succeeded" in keys:
        if keys != {"value", "succeeded"}:
            raise ProtocolError("show result has unknown or missing fields")
        _boolean(value["succeeded"], "show succeeded")
    elif keys == {"scope", "revision", "generation"}:
        _validated_scope(value["scope"])
        _positive(value["revision"], "revision")
        _nonnegative(value["generation"], "generation")
    elif "scope" in keys and ("revision" in keys or "generation" in keys):
        raise ProtocolError(
            "watchlist result must contain only scope/revision/generation"
        )


def _validate_error(value, outcome):
    _exact_fields(value, ("code", "message"))
    code = value["code"]
    if type(code) is not str or code not in _ERROR_MESSAGES:
        raise ProtocolError("unknown sanitized error code")
    if outcome == "aborted" and code != "ABORTED":
        raise ProtocolError("aborted results require the ABORTED error")
    if outcome == "failed" and code == "ABORTED":
        raise ProtocolError("failed results cannot use the ABORTED error")
    if value["message"] != _ERROR_MESSAGES[code]:
        raise ProtocolError("error message is not protocol-sanitized")


def validate_result(document):
    detached, _encoded = _decoded(document, MAX_RESULT_CANONICAL_BYTES)
    if type(detached) is not dict:
        raise ProtocolError("result must be an object")
    _job_id(detached.get("job_id"))
    outcome = detached.get("outcome")
    if type(outcome) is not str or outcome not in _OUTCOMES:
        raise ProtocolError("unknown result outcome")
    if outcome == "succeeded":
        _exact_fields(detached, ("job_id", "outcome", "value"))
        _validate_result_value(detached["value"])
    else:
        _exact_fields(detached, ("job_id", "outcome", "error"))
        _validate_error(detached["error"], outcome)
    return detached


def make_result(job_id, outcome, value=None):
    _job_id(job_id)
    if type(outcome) is not str or outcome not in _OUTCOMES:
        raise ProtocolError("unknown result outcome")
    if outcome == "succeeded":
        result = {
            "job_id": job_id,
            "outcome": outcome,
            "value": copy.deepcopy(value),
        }
    else:
        code = "ABORTED" if outcome == "aborted" else value
        if type(code) is not str:
            raise ProtocolError("failed result requires a sanitized error code")
        if outcome == "aborted" and value is not None and value != code:
            raise ProtocolError("aborted results require the ABORTED error")
        if outcome == "failed" and code == "ABORTED":
            raise ProtocolError("failed results cannot use the ABORTED error")
        if code not in _ERROR_MESSAGES:
            raise ProtocolError("unknown sanitized error code")
        result = {
            "job_id": job_id,
            "outcome": outcome,
            "error": {"code": code, "message": _ERROR_MESSAGES[code]},
        }
    return validate_result(result)
