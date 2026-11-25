"""Documented native WindowXML presentation for public discovery results."""

from types import MappingProxyType

from yawsp_app.tmdb import TMDBError


# Values from Kodi's published Action Id table, kept numeric for easy doubles.
ACTION_MOVE_LEFT = 1
ACTION_MOVE_RIGHT = 2
ACTION_MOVE_UP = 3
ACTION_MOVE_DOWN = 4
ACTION_SELECT_ITEM = 7
ACTION_PARENT_DIR = 9
ACTION_PREVIOUS_MENU = 10
ACTION_NAV_BACK = 92
_BACK_ACTIONS = frozenset(
    (ACTION_PARENT_DIR, ACTION_PREVIOUS_MENU, ACTION_NAV_BACK)
)


class DiscoveryWindow:
    """WindowXML callbacks and bounded state, independent of the Kodi import."""

    BACKDROP_ID = 900
    TITLE_ID = 1001
    CATEGORY_IDS = MappingProxyType(
        {
            1101: "popular_movies",
            1102: "popular_shows",
            1103: "top_rated_movies",
            1104: "top_rated_shows",
        }
    )
    RESULTS_ID = 1200
    RETRY_ID = 1300
    STATUS_ID = 1400
    DETAIL_TITLE_ID = 1500
    YEAR_ID = 1501
    GENRE_ID = 1502
    RATING_ID = 1503
    PLOT_ID = 1504

    def _initialize(self, category_labels, labels, load_category, item_factory):
        self._category_labels = dict(category_labels)
        self._labels = dict(labels)
        self._load_category = load_category
        self._item_factory = item_factory
        self._categories = dict(self.CATEGORY_IDS)
        self._sources = frozenset(self._categories.values())
        self._source = next(iter(self._sources))
        self._records = ()
        self._closed = False
        self._loading = False
        self.destination = None
        self.failed = False
        self.state = "loading"

        self.getControl(self.TITLE_ID).setLabel(self._labels["title"])
        for control_id, source in self._categories.items():
            self.getControl(control_id).setLabel(self._category_labels[source])
        self.getControl(self.STATUS_ID).setLabel("")
        self.getControl(self.STATUS_ID).setVisible(False)
        self.getControl(self.RETRY_ID).setLabel(self._labels["retry"])
        self.getControl(self.RETRY_ID).setVisible(False)
        self.getControl(self.RESULTS_ID).setVisible(False)
        self._clear_hero()

    def _clear_hero(self):
        for control_id in (
            self.DETAIL_TITLE_ID,
            self.YEAR_ID,
            self.GENRE_ID,
            self.RATING_ID,
        ):
            self.getControl(control_id).setLabel("")
        self.getControl(self.PLOT_ID).setText("")
        self.getControl(self.BACKDROP_ID).setImage("")

    def _focus_category(self):
        self.setFocus(self.getControl(next(iter(self.CATEGORY_IDS))))

    def _show_state(self, state):
        self.state = state
        has_content = state == "content"
        self.getControl(self.RESULTS_ID).setVisible(has_content)
        self.getControl(self.STATUS_ID).setVisible(not has_content)
        self.getControl(self.RETRY_ID).setVisible(state == "error")
        if state != "content":
            status = self._labels.get(state, "")
            self.getControl(self.STATUS_ID).setLabel(status)
        if state == "error":
            self.setFocus(self.getControl(self.RETRY_ID))
        elif state == "empty":
            self._focus_category()

    def load(self, source):
        """Synchronously replace the visible page; no background work or cache."""
        if self._closed or self._loading:
            return
        if source not in self._sources:
            self.failed = True
            self._show_state("error")
            return
        self._loading = True
        self._source = source
        self.failed = False
        self.destination = None
        self._records = ()
        self.clearList()
        self._clear_hero()
        self._show_state("loading")
        try:
            records = tuple(self._load_category(source))
        except TMDBError:
            self.failed = True
            self._show_state("error")
            return
        finally:
            self._loading = False

        self._records = records
        if not records:
            self._show_state("empty")
            return
        self.addItems([self._item_factory(record) for record in records])
        self.setCurrentListPosition(0)
        self._show_state("content")
        if self.getCurrentListPosition() < 0:
            self.setCurrentListPosition(0)
        self._show_selected()
        self.setFocus(self.getControl(self.RESULTS_ID))

    def _show_selected(self):
        if self.state != "content" or not self._records:
            return
        position = self.getCurrentListPosition()
        if type(position) is not int or not 0 <= position < len(self._records):
            return
        record = self._records[position]
        self.getControl(self.DETAIL_TITLE_ID).setLabel(record.title)
        self.getControl(self.YEAR_ID).setLabel(
            str(record.year) if record.year else ""
        )
        self.getControl(self.GENRE_ID).setLabel(" / ".join(record.genres))
        self.getControl(self.RATING_ID).setLabel(
            "{0:.1f}".format(record.rating)
        )
        self.getControl(self.PLOT_ID).setText(record.plot)
        self.getControl(self.BACKDROP_ID).setImage(record.art.get("fanart", ""))

    def _close_once(self):
        if self._closed:
            return
        self._closed = True
        self.close()

    def _activate(self):
        if self._closed or self.state != "content":
            return
        position = self.getCurrentListPosition()
        if type(position) is not int or not 0 <= position < len(self._records):
            return
        record = self._records[position]
        self.destination = record.search_url
        self._close_once()

    def onFocus(self, controlId):
        if not self._closed and controlId == self.RESULTS_ID:
            self._show_selected()

    def onClick(self, controlId):
        if self._closed or self._loading:
            return
        if controlId == self.RESULTS_ID:
            self._activate()
            return
        if controlId == self.RETRY_ID:
            self.load(self._source)
            return
        source = self._categories.get(controlId)
        if source is not None:
            self.load(source)

    def onAction(self, action):
        if self._closed:
            return
        action_id = action.getId()
        if action_id in _BACK_ACTIONS:
            self._close_once()
            return
        if action_id == ACTION_SELECT_ITEM:
            # WindowXML emits the corresponding onClick for Enter/OK. Handling
            # both here would submit the same destination twice.
            return
        if self.state != "content" or self.getFocusId() != self.RESULTS_ID:
            return
        if action_id in (ACTION_MOVE_LEFT, ACTION_MOVE_RIGHT):
            position = self.getCurrentListPosition()
            if type(position) is not int:
                return
            offset = -1 if action_id == ACTION_MOVE_LEFT else 1
            position = max(0, min(len(self._records) - 1, position + offset))
            self.setCurrentListPosition(position)
        if action_id in (
            ACTION_MOVE_LEFT,
            ACTION_MOVE_RIGHT,
            ACTION_MOVE_UP,
            ACTION_MOVE_DOWN,
        ):
            self._show_selected()


def create_discovery_window(
    addon_path,
    category_labels,
    labels,
    load_category,
    item_factory,
    window_base=None,
):
    """Create the real WindowXML, with a base-class seam only for offline tests."""
    if window_base is None:
        import xbmcgui

        window_base = xbmcgui.WindowXML

    class NativeDiscoveryWindow(DiscoveryWindow, window_base):
        def __init__(self):
            window_base.__init__(
                self, "DiscoveryWindow.xml", addon_path, "default", "720p"
            )
            self._initialize(category_labels, labels, load_category, item_factory)

    return NativeDiscoveryWindow()
