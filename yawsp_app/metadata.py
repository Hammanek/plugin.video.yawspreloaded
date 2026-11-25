"""Apply the add-on's video metadata through Kodi's native InfoTagVideo API."""


_VIDEO_SETTERS = (
    ("title", "setTitle"),
    ("mediatype", "setMediaType"),
    ("plot", "setPlot"),
    ("year", "setYear"),
    ("genre", "setGenres"),
    ("rating", "setRating"),
    ("season", "setSeason"),
    ("episode", "setEpisode"),
    ("duration", "setDuration"),
    ("aired", "setFirstAired"),
)


def apply_video_metadata(listitem, info):
    """Apply only supported metadata, retaining legacy semantics for old doubles."""
    get_video_info_tag = getattr(listitem, "getVideoInfoTag", None)
    if get_video_info_tag is None:
        listitem.setInfo("video", info)
        return

    video_tag = get_video_info_tag()
    supported = {key for key, _ in _VIDEO_SETTERS}
    unsupported = sorted(str(key) for key in info if key not in supported)
    if unsupported:
        raise ValueError(
            "Unsupported native video metadata keys: " + ", ".join(unsupported)
        )

    required_setters = []
    for key, setter_name in _VIDEO_SETTERS:
        if key not in info:
            continue
        setter = getattr(video_tag, setter_name, None)
        if not callable(setter):
            # Kodi 19 has getVideoInfoTag but not the v20 dedicated setters.
            listitem.setInfo("video", info)
            return
        required_setters.append((key, setter))

    for key, setter in required_setters:
        value = info[key]
        if key == "genre":
            value = value.split(" / ") if isinstance(value, str) else list(value)
        setter(value)
