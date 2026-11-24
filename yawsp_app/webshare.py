"""Validated Webshare transport and authentication without Kodi dependencies."""

from dataclasses import dataclass
import hashlib
import logging
from typing import Tuple
import uuid
from xml.etree import ElementTree as ET

import requests

from md5crypt import md5crypt


_REALM = b":Webshare:"
_TIMEOUT = 30
_LOGGER = logging.getLogger(__name__)


class WebshareError(RuntimeError):
    """A Webshare transport failure or invalid API response."""


@dataclass(frozen=True)
class XmlSearchPage:
    """Valid raw result elements and the count before invalid items are skipped."""

    files: Tuple[ET.Element, ...]
    raw_count: int


def _required_text(xml, tag):
    element = xml.find(tag)
    if element is None or element.text is None or not element.text.strip():
        raise WebshareError("Webshare response is missing required <{0}>".format(tag))
    return element.text


def _is_ok(xml):
    return _required_text(xml, "status") == "OK"


class WebshareClient:
    def __init__(self, session, settings, api_url="https://webshare.cz/api/"):
        self.session = session
        self.settings = settings
        self.api_url = api_url if api_url.endswith("/") else api_url + "/"

    def _post(self, endpoint, data):
        try:
            response = self.session.post(
                self.api_url + endpoint + "/", data=data, timeout=_TIMEOUT
            )
            response.raise_for_status()
        except requests.RequestException as error:
            raise WebshareError(
                "Webshare {0} request failed: {1}".format(endpoint, error)
            ) from error

        try:
            return ET.fromstring(response.content)
        except ET.ParseError as error:
            raise WebshareError(
                "Webshare {0} returned malformed XML".format(endpoint)
            ) from error

    def _reject_login(self):
        self.settings.set("token", "")
        return None

    def login(self):
        username = self.settings.get("wsuser")
        password = self.settings.get("wspass")
        if not username or not password:
            return self._reject_login()

        salt_xml = self._post("salt", {"username_or_email": username})
        if not _is_ok(salt_xml):
            return self._reject_login()
        salt = _required_text(salt_xml, "salt")

        encrypted_password = hashlib.sha1(
            md5crypt(password.encode("utf-8"), salt.encode("utf-8")).encode("utf-8")
        ).hexdigest()
        digest = hashlib.md5(
            username.encode("utf-8") + _REALM + encrypted_password.encode("utf-8")
        ).hexdigest()
        login_xml = self._post(
            "login",
            {
                "username_or_email": username,
                "password": encrypted_password,
                "digest": digest,
                "keep_logged_in": 1,
            },
        )
        if not _is_ok(login_xml):
            return self._reject_login()

        token = _required_text(login_xml, "token")
        self.settings.set("token", token)
        return token

    def revalidate(self):
        token = self.settings.get("token")
        if not token:
            return self.login()

        user_xml = self._post("user_data", {"wst": token})
        if not _is_ok(user_xml):
            self.settings.set("token", "")
            return self.login()

        _required_text(user_xml, "vip")
        return token

    def search(self, token, query, category, sort, limit, offset):
        xml = self._post(
            "search",
            {
                "what": query.api_text,
                "category": category,
                "sort": sort,
                "limit": limit,
                "offset": offset,
                "wst": token,
                "maybe_removed": "true",
            },
        )
        if not _is_ok(xml):
            raise WebshareError("Webshare search rejected the request")

        raw_files = list(xml.iter("file"))
        valid_files = []
        for file_element in raw_files:
            try:
                _required_text(file_element, "name")
                _required_text(file_element, "ident")
            except WebshareError as error:
                _LOGGER.warning("Skipping invalid Webshare search result: %s", error)
            else:
                valid_files.append(file_element)
        return XmlSearchPage(files=tuple(valid_files), raw_count=len(raw_files))

    def get_info(self, ident, token):
        xml = self._post("file_info", {"ident": ident, "wst": token})
        if not _is_ok(xml):
            xml = self._post(
                "file_info",
                {"ident": ident, "wst": token, "maybe_removed": "true"},
            )
            if not _is_ok(xml):
                raise WebshareError("Webshare file info rejected the request")

        _required_text(xml, "name")
        _required_text(xml, "ident")
        return xml

    def get_link(self, ident, token, dtype="video_stream"):
        device_uuid = self.settings.get("duuid")
        if not device_uuid:
            device_uuid = str(uuid.uuid4())
            self.settings.set("duuid", device_uuid)

        xml = self._post(
            "file_link",
            {
                "ident": ident,
                "wst": token,
                "download_type": dtype,
                "device_uuid": device_uuid,
            },
        )
        if not _is_ok(xml):
            raise WebshareError("Webshare file link rejected the request")
        return _required_text(xml, "link")
