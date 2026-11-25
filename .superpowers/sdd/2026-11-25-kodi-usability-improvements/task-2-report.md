# Task 2 report — native video metadata adapter

## Scope and API evidence

- Added `yawsp_app/metadata.py` with the narrowly scoped `apply_video_metadata(listitem, info)` adapter and replaced all **six actual current** video `ListItem.setInfo("video", ...)` call sites: three in `webshare_ui.py` and three in `tmdb_ui.py`. The brief said seven; source inspection found six before editing.
- Did not edit `tmdb.py`, routes, settings, shared caches/types, artwork construction, playback behavior, main-profile data, release files, or the other agent's transport work.
- Kodi's public documentation was checked directly, rather than deriving setter names from the legacy stubs:
  - InfoTagVideo API: <https://xbmc.github.io/docs.kodi.tv/master/kodi-base/d9/dc2/group__python___info_tag_video.html>
  - Python v20 API changes introducing the dedicated setters and partially deprecating video `ListItem.setInfo`: <https://xbmc.github.io/docs.kodi.tv/master/kodi-base/de/d1e/python_v20.html>
  - The docs specify `setGenres(genre: list)`, `setDuration(duration: integer)` in **seconds**, `setRating(rating: float)`, and `setFirstAired(firstaired: string)`.

## Behavior mapping and contract choices

| Input key | Native call | Preservation/normalization |
|---|---|---|
| `title` | `setTitle` | Original string, including localized UTF-8 and existing color markup |
| `mediatype` | `setMediaType` | Original string |
| `plot` | `setPlot` | Original string |
| `year` | `setYear` | Existing integer value; no new conversion |
| `genre` | `setGenres` | Strings split on exact `" / "`; list/tuple inputs copied to a new list |
| `rating` | `setRating` | Existing float value, including `7.25`; no rounding/string conversion |
| `season` | `setSeason` | Existing integer value |
| `episode` | `setEpisode` | Existing integer value; season rows retain the existing episode-count mapping |
| `duration` | `setDuration` | Existing seconds; the episode UI's existing minutes × 60 calculation remains upstream |
| `aired` | `setFirstAired` | Original `YYYY-MM-DD`; deliberately not `setPremiered` |

- No other setters, labels, paths, or metadata keys were added.
- **Legacy choice:** only an absent/`None` `getVideoInfoTag` attribute selects `listitem.setInfo("video", info)`. It receives the original dictionary object exactly once, including unsupported legacy keys, retaining old Kodi/default semantics without mutation.
- **Native unsupported-key choice:** unknown and case-mismatched keys raise `ValueError` before any supported setter runs. This prevents silent field loss or partial metadata degradation. Existing production calls use only the supported keys above.
- **Missing-setter choice:** there is no per-setter fallback and no broad `AttributeError` catch. A native tag missing a documented setter raises rather than pretending to be a legacy double.

## TDD / RED evidence

1. Original baseline at `d7e4213`: `python -m unittest discover -s tests -v` — **333 completed, 333 passed, OK**.
2. Before `yawsp_app/metadata.py` existed: `python -m unittest tests.test_metadata -v` — **1 loader error**, specifically `ModuleNotFoundError: No module named 'yawsp_app.metadata'`. This was the expected missing-production-feature RED, not a fake InfoTag API failure.
3. After implementing only the adapter and before changing either UI, the exact nine-selector focused run completed **9 tests: 5 passed and 4 failed**. All four failures were `AssertionError: native rows must not use ListItem.setInfo` at the then-current production Webshare/TMDB calls; all five adapter tests already passed.
4. After replacing the six UI calls, the first exact nine-selector run completed **8 passed, 1 failed**. The sole failure was the episode test's hand-written expected call order (`rating` had been listed after duration while the shared mapping applies it before season/duration); the expected order was corrected, not production behavior.
5. Final exact focused selector run: **9 completed, 9 passed, OK**.

Final focused command:

```text
python -m unittest -v tests.test_metadata tests.test_webshare_ui.WebshareUITests.test_native_file_row_sets_filename_title_without_legacy_info tests.test_webshare_ui.WebshareUITests.test_native_backup_rows_preserve_selected_title_and_plot tests.test_tmdb_ui.TMDBUITests.test_native_watchlist_movies_and_shows_preserve_all_display_metadata tests.test_tmdb_ui.TMDBUITests.test_native_season_and_episode_rows_keep_counts_dates_runtime_and_labels
```

## Row and fallback coverage

- Webshare native file rows set the filename-based title, retain thumbnail art and playability, and never call legacy `setInfo`.
- Backup rows preserve selected title and UTF-8 plot metadata; selected stream title and playability remain intact, with no authentication/search requests introduced by presentation.
- TMDB native movie and show rows preserve title/year/media type/plot, genre list conversion, exact float rating, movie artwork and existing navigation.
- Native season rows preserve season number and episode count. Native episode rows preserve the colorized date label, UTF-8 title/plot, season/episode, 3000-second duration, rating, thumbnail, query, and `setFirstAired`.
- Existing legacy-double row tests continue to pass, including duration/aired, today/unreleased filtering, status/empty rows, thumbnails, and route behavior.
- The direct fallback test proves exactly one legacy invocation with the same dictionary object. The incomplete-native-tag test proves that missing setters do not trigger fallback.

## Final verification counts and outcomes

- `python -m unittest discover -s tests -v`: **344 completed, 344 passed, OK** (333 original + 9 Task 2 tests + 2 transport-redaction tests committed concurrently as `63cd17f`).
- Repeated `python -m unittest discover -s tests`: **344 completed, 344 passed, OK**.
- Python `ast.parse(..., feature_version=(3, 8))`: **6/6 touched Python files parsed successfully**.
- `git diff --check`: **0 findings**.
- Source inspection after migration: UI files contain **0** `setInfo` calls; the only one among the three production files is the intentional single legacy fallback in `metadata.py`.

## Limitations / statistics

- Tests use explicit doubles exposing only the documented methods. No actual Kodi GUI invocation or on-device visual comparison was performed in this task, and no claim is made about it.
- No performance benchmark was run; no performance improvement is claimed. The implementation adds no media requests and does not change cache ownership or types.
