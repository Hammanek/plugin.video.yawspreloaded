"""One-page public TMDB discovery data and native directory presentation."""

from dataclasses import dataclass
import math
from types import MappingProxyType

from yawsp_app.metadata import apply_video_metadata
from yawsp_app.runtime import DirectoryResult
from yawsp_app.tmdb import TMDBError


_IMAGE_URL = "https://image.tmdb.org/t/p/"


@dataclass(frozen=True)
class _CatalogSource:
    path: str
    media: str
    content: str


CATALOG_SOURCES = MappingProxyType(
    {
        "popular_movies": _CatalogSource("/movie/popular", "movie", "movies"),
        "popular_shows": _CatalogSource("/tv/popular", "tv", "tvshows"),
        "top_rated_movies": _CatalogSource(
            "/movie/top_rated", "movie", "movies"
        ),
        "top_rated_shows": _CatalogSource("/tv/top_rated", "tv", "tvshows"),
    }
)


@dataclass(frozen=True)
class DiscoveryRecord:
    """Detached display/search values shared by native discovery rows."""

    media: str
    media_id: int
    title: str
    original_title: str
    year: int
    genres: tuple
    rating: float
    plot: str
    art: object
    search_url: str
    original_search_url: str

    @property
    def label(self):
        return "{0} ({1})".format(self.title, self.year) if self.year else self.title


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _year(value):
    value = value if isinstance(value, str) else ""
    return int(value[:4]) if len(value) >= 4 and value[:4].isdigit() else 0


def _rating(value):
    if isinstance(value, bool):
        return 0.0
    try:
        rating = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return rating if math.isfinite(rating) else 0.0


def _media_id(value):
    if isinstance(value, bool):
        return None
    if type(value) is int:
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _image(size, path):
    path = _text(path)
    return _IMAGE_URL + size + path if path else ""


def map_discovery_record(source, value, genre_map, unknown_title=""):
    """Return only detached response values; navigation stays in the parent."""
    if not isinstance(value, dict):
        return None
    media_id = _media_id(value.get("id"))
    if media_id is None:
        return None
    is_movie = source.media == "movie"
    title = _text(value.get("title") if is_movie else value.get("name"))
    original_title = _text(
        value.get("original_title") if is_movie else value.get("original_name")
    )
    title = title or original_title or unknown_title
    original_title = original_title or title
    date_value = value.get("release_date") if is_movie else value.get("first_air_date")
    raw_genres = value.get("genre_ids")
    genre_values = []
    if isinstance(raw_genres, (list, tuple)) and isinstance(genre_map, dict):
        for genre_id in raw_genres:
            genre = genre_map.get(genre_id)
            if isinstance(genre, str) and genre:
                genre_values.append(genre)

    artwork = {
        key: url
        for key, url in (
            ("poster", _image("w500", value.get("poster_path"))),
            ("thumb", _image("w500", value.get("poster_path"))),
            ("fanart", _image("w780", value.get("backdrop_path"))),
        )
        if url
    }
    return {
        "media": source.media,
        "media_id": media_id,
        "title": title,
        "original_title": original_title,
        "year": _year(date_value),
        "genres": genre_values,
        "rating": _rating(value.get("vote_average")),
        "plot": _text(value.get("overview")),
        "art": artwork,
    }


def _normalize_record(runtime, url_builder, source, value, genre_map):
    values = map_discovery_record(
        source, value, genre_map, runtime.localized(30408)
    )
    if values is None:
        return None
    search_url = url_builder(
        runtime, action="search", what=values["title"], sort="largest"
    )
    original_search_url = url_builder(
        runtime, action="search", what=values["original_title"], sort="largest"
    )
    return DiscoveryRecord(
        genres=tuple(values["genres"]),
        art=MappingProxyType(values["art"]),
        search_url=search_url,
        original_search_url=original_search_url,
        **{
            key: item
            for key, item in values.items()
            if key not in ("genres", "art")
        },
    )


def discovery_list_item(runtime, record):
    """Build one native row using Kodi's current InfoTag metadata adapter."""
    item = runtime.list_item(label=record.label)
    if record.art:
        item.setArt(dict(record.art))
    if record.original_search_url != record.search_url:
        item.addContextMenuItems(
            [
                (
                    runtime.localized(30414),
                    "Container.Update({0})".format(record.original_search_url),
                )
            ]
        )
    apply_video_metadata(
        item,
        {
            "title": record.label,
            "mediatype": "movie" if record.media == "movie" else "tvshow",
            "plot": record.plot,
            "year": record.year,
            "genre": " / ".join(record.genres),
            "rating": record.rating,
        },
    )
    return item


def build_discovery_handlers(
    runtime,
    client,
    url_builder,
    genres,
    set_default_view,
    valid_configuration=None,
):
    """Wire one explicit discovery route without personal or background work."""

    def language():
        mapper = getattr(client, "locale_mapper", None)
        return mapper() if mapper is not None else "en-US"

    def load_category(source_name):
        source = CATALOG_SOURCES[source_name]
        try:
            data = client.get(source.path)
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise TMDBError("TMDB discovery response has no results array")
            values = data["results"]
            if not values:
                return ()
            genre_map = genres(source.media, language())
            return tuple(
                record
                for record in (
                    _normalize_record(
                        runtime, url_builder, source, value, genre_map
                    )
                    for value in values
                )
                if record is not None
            )
        except TMDBError:
            runtime.logger.exception("TMDB discovery listing failed")
            raise
        except (TypeError, ValueError, OverflowError) as error:
            runtime.logger.exception("TMDB discovery response normalization failed")
            raise TMDBError("TMDB discovery response could not be displayed") from error

    def category_labels():
        return MappingProxyType(
            {
                "popular_movies": "{0} {1}".format(
                    runtime.localized(30442), runtime.localized(30421)
                ),
                "popular_shows": "{0} {1}".format(
                    runtime.localized(30442), runtime.localized(30422)
                ),
                "top_rated_movies": "{0} {1}".format(
                    runtime.localized(30443), runtime.localized(30421)
                ),
                "top_rated_shows": "{0} {1}".format(
                    runtime.localized(30443), runtime.localized(30422)
                ),
            }
        )

    def item_factory(record):
        return discovery_list_item(runtime, record)

    def native(source_name, source, params):
        try:
            records = load_category(source_name)
        except TMDBError:
            runtime.notify(runtime.localized(30402))
            return DirectoryResult(succeeded=False)

        runtime.set_category(category_labels()[source_name])
        for record in records:
            runtime.add_item(
                record.search_url, item_factory(record), True
            )
        if not records:
            item = runtime.list_item(label=runtime.localized(30440))
            runtime.add_item(
                url_builder(
                    runtime, action="tmdb_discovery", source=source_name
                ),
                item,
            )
        runtime.set_content(source.content)

        def post_complete():
            set_default_view(
                {"category": "movies" if source.media == "movie" else "shows"}
            )

        return DirectoryResult(post_complete=post_complete)

    def discovery(params):
        params = params or {}
        if "source" not in params:
            for source_name, label in category_labels().items():
                item = runtime.list_item(label=label)
                runtime.add_item(
                    url_builder(
                        runtime, action="tmdb_discovery", source=source_name
                    ),
                    item,
                    True,
                )
            return DirectoryResult()
        source_name = params.get("source")
        if not isinstance(source_name, str):
            return DirectoryResult(succeeded=False)
        source = CATALOG_SOURCES.get(source_name)
        if source is None:
            return DirectoryResult(succeeded=False)
        if valid_configuration is not None and not valid_configuration():
            return DirectoryResult(succeeded=False)
        return native(source_name, source, params)

    return {"tmdb_discovery": discovery}
