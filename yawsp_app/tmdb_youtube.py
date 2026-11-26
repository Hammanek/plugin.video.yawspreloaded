"""Conservative YouTube video identifiers and builtin command construction."""

import re
from urllib.parse import urlencode


_YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}")


def valid_youtube_id(value):
    """Accept exactly YouTube's eleven unconstrained URL-safe identifier chars."""

    return type(value) is str and _YOUTUBE_ID.fullmatch(value) is not None


def youtube_play_media(value):
    """Build a PlayMedia command only from an already strict identifier."""

    if not valid_youtube_id(value):
        raise ValueError("invalid YouTube video identifier")
    return "PlayMedia(plugin://plugin.video.youtube/play/?{0})".format(
        urlencode({"video_id": value})
    )
