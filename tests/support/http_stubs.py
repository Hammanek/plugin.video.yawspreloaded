"""In-memory HTTP test doubles; exhausted sessions never perform network I/O."""

from collections import deque
import json

from requests import HTTPError


_JSON_NOT_PROVIDED = object()


class FakeResponse:
    def __init__(
        self,
        content=b"",
        status_code=200,
        headers=None,
        chunks=None,
        json_data=_JSON_NOT_PROVIDED,
        url="https://example.invalid/",
        stream_error=None,
        forbid_content=False,
    ):
        if isinstance(content, str):
            content = content.encode("utf-8")
        self._content = bytes(content)
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.url = url
        self.closed = False
        self.raise_for_status_calls = 0
        self._chunks = None if chunks is None else [bytes(chunk) for chunk in chunks]
        if self._chunks is not None and not self._content:
            self._content = b"".join(self._chunks)
        self._json_data = json_data
        self._stream_error = stream_error
        self._forbid_content = forbid_content

    @property
    def content(self):
        if self._forbid_content:
            raise AssertionError("response.content access is forbidden for streaming")
        return self._content

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        if self._json_data is not _JSON_NOT_PROVIDED:
            return self._json_data
        return json.loads(self.content.decode("utf-8"))

    def raise_for_status(self):
        self.raise_for_status_calls += 1
        if self.status_code >= 400:
            raise HTTPError(
                "HTTP {0} for {1}".format(self.status_code, self.url), response=self
            )

    def iter_content(self, chunk_size=1, decode_unicode=False):
        if chunk_size is None or chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer")
        chunks = self._chunks
        if chunks is None:
            chunks = [self._content] if self._content else []
        for chunk in chunks:
            if decode_unicode:
                yield chunk.decode("utf-8", errors="replace")
            else:
                yield chunk
        if self._stream_error is not None:
            raise self._stream_error

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class FakeSession:
    """Return queued responses/exceptions and record every attempted request."""

    def __init__(self, responses=()):
        self.responses = deque(responses)
        self.calls = []
        self.closed = False

    def queue_response(self, response):
        self.responses.append(response)

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method.upper(), "url": url, "kwargs": kwargs})
        if not self.responses:
            raise AssertionError("FakeSession has no queued response; network is disabled")
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self.request("PUT", url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("DELETE", url, **kwargs)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
