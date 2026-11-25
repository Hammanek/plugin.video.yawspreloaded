import os
import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace

from yawsp_app.discovery_window import (
    ACTION_MOVE_DOWN,
    ACTION_MOVE_LEFT,
    ACTION_MOVE_RIGHT,
    ACTION_MOVE_UP,
    ACTION_NAV_BACK,
    ACTION_PARENT_DIR,
    ACTION_PREVIOUS_MENU,
    ACTION_SELECT_ITEM,
    DiscoveryWindow,
    create_discovery_window,
)
from yawsp_app.tmdb import TMDBError


CATEGORY_LABELS = {
    "popular_movies": "Populární Filmy",
    "popular_shows": "Populární Seriály",
    "top_rated_movies": "Nejlépe hodnocené Filmy",
    "top_rated_shows": "Nejlépe hodnocené Seriály",
}
LABELS = {
    "title": "Objevit",
    "loading": "Načítání…",
    "empty": "Žádné výsledky",
    "error": "Chyba načítání",
    "retry": "Obnovit",
}


def record(number, title):
    return SimpleNamespace(
        media="movie",
        media_id=number,
        title=title,
        original_title="Original " + title,
        year=2020 + number,
        genres=("Drama", "Komedie"),
        rating=6.5 + number,
        plot="Plot for " + title,
        art={
            "poster": "https://image/poster-{0}.jpg".format(number),
            "thumb": "https://image/poster-{0}.jpg".format(number),
            "fanart": "https://image/backdrop-{0}.jpg".format(number),
        },
        search_url="plugin://plugin.video.yawspreloaded/?action=search&what={0}&sort=largest".format(title),
        original_search_url="plugin://plugin.video.yawspreloaded/?action=search&what=Original+{0}&sort=largest".format(title),
    )


class Action:
    def __init__(self, action_id):
        self.action_id = action_id

    def getId(self):
        return self.action_id


class Control:
    def __init__(self):
        self.label = ""
        self.text = ""
        self.image = ""
        self.visible = True
        self.label_calls = []
        self.text_calls = []
        self.image_calls = []
        self.visible_calls = []

    def setLabel(self, label):
        self.label = label
        self.label_calls.append(label)

    def setText(self, text):
        self.text = text
        self.text_calls.append(text)

    def setImage(self, image):
        self.image = image
        self.image_calls.append(image)

    def setVisible(self, visible):
        self.visible = visible
        self.visible_calls.append(visible)


class ListItem:
    def __init__(self, label="", art=None):
        self.label = label
        self.art = dict(art or {})


class WindowXML:
    """Only real WindowXML/Window methods used by the production class."""

    def __init__(self, xml_filename, script_path, default_skin, default_res):
        self.init_args = (xml_filename, script_path, default_skin, default_res)
        self.controls = {}
        self.items = []
        self.position = -1
        self.focus_id = 0
        self.close_calls = 0
        self.modal_calls = 0
        self.focus_history = []

    def getControl(self, control_id):
        return self.controls.setdefault(control_id, Control())

    def setFocus(self, control):
        focus = [key for key, value in self.controls.items() if value is control]
        if len(focus) != 1:
            raise AssertionError("focused control must belong to this window")
        self.setFocusId(focus[0])

    def setFocusId(self, control_id):
        self.focus_id = control_id
        self.focus_history.append(control_id)

    def getFocusId(self):
        return self.focus_id

    def addItems(self, items):
        self.items.extend(items)
        if self.items and self.position < 0:
            self.position = 0

    def addItem(self, item, position=None):
        if position is None:
            self.items.append(item)
        else:
            self.items.insert(position, item)
        if self.items and self.position < 0:
            self.position = 0

    def clearList(self):
        self.items = []
        self.position = -1

    def getCurrentListPosition(self):
        return self.position

    def setCurrentListPosition(self, position):
        self.position = position

    def getListSize(self):
        return len(self.items)

    def doModal(self):
        self.modal_calls += 1

    def close(self):
        self.close_calls += 1


class DiscoveryWindowTests(unittest.TestCase):
    def make_window(self, responses):
        self.remaining = list(responses)
        self.loads = []
        self.loading_observations = []
        holder = {}

        def load_category(source):
            self.loads.append(source)
            window = holder.get("window")
            if window is not None:
                self.loading_observations.append(
                    (window.state, window.getControl(DiscoveryWindow.STATUS_ID).label)
                )
            response = self.remaining.pop(0)
            if isinstance(response, BaseException):
                raise response
            return list(response)

        def item_factory(value):
            return ListItem(value.title, value.art)

        window = create_discovery_window(
            addon_path="C:\\addon",
            category_labels=CATEGORY_LABELS,
            labels=LABELS,
            load_category=load_category,
            item_factory=item_factory,
            window_base=WindowXML,
        )
        holder["window"] = window
        return window

    def test_factory_uses_documented_windowxml_and_injects_all_category_labels(self):
        window = self.make_window([])

        self.assertIsInstance(window, WindowXML)
        self.assertEqual(
            window.init_args, ("DiscoveryWindow.xml", "C:\\addon", "default", "720p")
        )
        for control_id, source in DiscoveryWindow.CATEGORY_IDS.items():
            self.assertEqual(window.getControl(control_id).label, CATEGORY_LABELS[source])
        self.assertEqual(window.getControl(DiscoveryWindow.TITLE_ID).label, "Objevit")
        self.assertEqual(window.modal_calls, 0)
        self.assertEqual(window.close_calls, 0)

    def test_loading_precedes_data_then_content_is_added_once_without_focus_requests(self):
        window = self.make_window([[record(1, "One"), record(2, "Two")]])
        window.load("popular_movies")

        self.assertEqual(self.loads, ["popular_movies"])
        self.assertEqual(self.loading_observations, [("loading", "Načítání…")])
        self.assertEqual(window.state, "content")
        self.assertFalse(window.failed)
        self.assertEqual([item.label for item in window.items], ["One", "Two"])
        self.assertEqual(window.position, 0)
        self.assertFalse(window.getControl(DiscoveryWindow.RETRY_ID).visible)

        window.onFocus(DiscoveryWindow.RESULTS_ID)
        window.setFocus(window.getControl(DiscoveryWindow.RESULTS_ID))
        window.onAction(Action(ACTION_MOVE_RIGHT))
        window.onAction(Action(ACTION_MOVE_LEFT))
        window.onAction(Action(ACTION_MOVE_UP))
        window.onAction(Action(ACTION_MOVE_DOWN))
        self.assertEqual(self.loads, ["popular_movies"])

    def test_navigation_updates_all_hero_fields_and_selection_stays_bounded(self):
        window = self.make_window([[record(1, "One"), record(2, "Two")]])
        window.load("popular_movies")

        window.onAction(Action(ACTION_MOVE_RIGHT))
        self.assertEqual(window.position, 1)
        self.assertEqual(window.getControl(DiscoveryWindow.DETAIL_TITLE_ID).label, "Two")
        self.assertEqual(window.getControl(DiscoveryWindow.YEAR_ID).label, "2022")
        self.assertEqual(window.getControl(DiscoveryWindow.GENRE_ID).label, "Drama / Komedie")
        self.assertEqual(window.getControl(DiscoveryWindow.RATING_ID).label, "8.5")
        self.assertEqual(window.getControl(DiscoveryWindow.PLOT_ID).text, "Plot for Two")
        self.assertEqual(
            window.getControl(DiscoveryWindow.BACKDROP_ID).image,
            "https://image/backdrop-2.jpg",
        )
        window.onAction(Action(ACTION_MOVE_RIGHT))
        self.assertEqual(window.position, 1)
        window.onAction(Action(ACTION_MOVE_LEFT))
        window.onAction(Action(ACTION_MOVE_LEFT))
        self.assertEqual(window.position, 0)
        self.assertEqual(window.getControl(DiscoveryWindow.DETAIL_TITLE_ID).label, "One")
        self.assertEqual(self.loads, ["popular_movies"])

    def test_each_category_activation_loads_once_but_focus_does_not(self):
        window = self.make_window([[record(1, "One")], [], [record(3, "Three")]])

        first_id = next(iter(DiscoveryWindow.CATEGORY_IDS))
        window.onClick(first_id)
        window.onClick(first_id)
        self.assertEqual(self.loads, ["popular_movies", "popular_movies"])
        window.onFocus(first_id)
        self.assertEqual(len(self.loads), 2)

        last_id = next(reversed(DiscoveryWindow.CATEGORY_IDS))
        window.onClick(last_id)
        self.assertEqual(self.loads[-1], "top_rated_shows")
        self.assertEqual([item.label for item in window.items], ["Three"])

    def test_enter_close_and_repeated_callbacks_hand_off_once(self):
        window = self.make_window([[record(1, "One"), record(2, "Two")]])
        window.load("popular_movies")
        window.onAction(Action(ACTION_MOVE_RIGHT))

        window.onAction(Action(ACTION_SELECT_ITEM))
        window.onClick(DiscoveryWindow.RESULTS_ID)
        window.onAction(Action(ACTION_SELECT_ITEM))
        window.onClick(DiscoveryWindow.RESULTS_ID)

        self.assertEqual(window.destination, record(2, "Two").search_url)
        self.assertEqual(window.close_calls, 1)
        self.assertEqual(self.loads, ["popular_movies"])

    def test_back_closes_once_without_selection_for_documented_back_actions(self):
        for action_id in (ACTION_NAV_BACK, ACTION_PREVIOUS_MENU, ACTION_PARENT_DIR):
            with self.subTest(action_id=action_id):
                window = self.make_window([[record(1, "One")]])
                window.load("popular_movies")
                window.onAction(Action(action_id))
                window.onAction(Action(action_id))
                window.onClick(DiscoveryWindow.RESULTS_ID)
                self.assertEqual(window.close_calls, 1)
                self.assertIsNone(window.destination)

    def test_closed_window_cannot_refetch_or_select_from_late_callbacks(self):
        window = self.make_window([[record(1, "One")]])
        window.load("popular_movies")
        window.onClick(DiscoveryWindow.RESULTS_ID)
        before = list(self.loads)

        window.onClick(next(iter(DiscoveryWindow.CATEGORY_IDS)))
        window.onClick(DiscoveryWindow.RETRY_ID)
        window.onClick(DiscoveryWindow.RESULTS_ID)
        window.onAction(Action(ACTION_MOVE_RIGHT))

        self.assertEqual(self.loads, before)
        self.assertEqual(window.close_calls, 1)

    def test_error_then_visible_retry_recovers_without_closing_or_fake_results(self):
        window = self.make_window([TMDBError("synthetic"), [record(4, "Recovered")]])
        window.load("popular_movies")

        self.assertEqual(self.loading_observations[0], ("loading", "Načítání…"))
        self.assertEqual(window.state, "error")
        self.assertTrue(window.failed)
        self.assertEqual(window.getControl(DiscoveryWindow.STATUS_ID).label, "Chyba načítání")
        self.assertEqual(window.items, [])
        self.assertTrue(window.getControl(DiscoveryWindow.RETRY_ID).visible)
        self.assertEqual(window.focus_id, DiscoveryWindow.RETRY_ID)
        self.assertEqual(window.close_calls, 0)

        window.onClick(DiscoveryWindow.RETRY_ID)
        self.assertEqual(window.state, "content")
        self.assertEqual([item.label for item in window.items], ["Recovered"])
        self.assertFalse(window.getControl(DiscoveryWindow.RETRY_ID).visible)
        self.assertEqual(window.close_calls, 0)
        self.assertEqual(self.loads, ["popular_movies"] * 2)

    def test_empty_has_explanatory_state_and_reachable_category_focus(self):
        window = self.make_window([[]])
        window.load("popular_movies")

        self.assertEqual(window.state, "empty")
        self.assertFalse(window.failed)
        self.assertEqual(window.getControl(DiscoveryWindow.STATUS_ID).label, "Žádné výsledky")
        self.assertFalse(window.getControl(DiscoveryWindow.RETRY_ID).visible)
        self.assertEqual(window.focus_id, next(iter(DiscoveryWindow.CATEGORY_IDS)))
        self.assertEqual(window.items, [])
        self.assertEqual(window.close_calls, 0)

    def test_documented_action_ids_match_kodi_common_values(self):
        self.assertEqual(
            (
                ACTION_MOVE_LEFT,
                ACTION_MOVE_RIGHT,
                ACTION_MOVE_UP,
                ACTION_MOVE_DOWN,
                ACTION_SELECT_ITEM,
                ACTION_PARENT_DIR,
                ACTION_PREVIOUS_MENU,
                ACTION_NAV_BACK,
            ),
            (1, 2, 3, 4, 7, 9, 10, 92),
        )


class DiscoveryWindowXMLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "resources",
            "skins",
            "default",
            "720p",
            "DiscoveryWindow.xml",
        )
        cls.tree = ET.parse(path)
        cls.root = cls.tree.getroot()
        cls.controls = {
            int(control.attrib["id"]): control
            for control in cls.root.findall("./controls/control")
            if "id" in control.attrib
        }

    def child(self, control_id, tag):
        value = self.controls[control_id].find(tag)
        self.assertIsNotNone(value, "missing {0} for control {1}".format(tag, control_id))
        return value

    def test_xml_ids_types_and_focus_layout_match_production_constants(self):
        expected_types = {
            DiscoveryWindow.BACKDROP_ID: "image",
            DiscoveryWindow.TITLE_ID: "label",
            DiscoveryWindow.RESULTS_ID: "panel",
            DiscoveryWindow.STATUS_ID: "label",
            DiscoveryWindow.RETRY_ID: "button",
            DiscoveryWindow.DETAIL_TITLE_ID: "label",
            DiscoveryWindow.YEAR_ID: "label",
            DiscoveryWindow.GENRE_ID: "label",
            DiscoveryWindow.RATING_ID: "label",
            DiscoveryWindow.PLOT_ID: "textbox",
        }
        expected_types.update(
            {control_id: "button" for control_id in DiscoveryWindow.CATEGORY_IDS}
        )
        for control_id, control_type in expected_types.items():
            with self.subTest(control_id=control_id):
                self.assertEqual(self.controls[control_id].attrib["type"], control_type)

        results = self.controls[DiscoveryWindow.RESULTS_ID]
        item_layout = results.find("itemlayout")
        focused_layout = results.find("focusedlayout")
        self.assertIsNotNone(item_layout)
        self.assertIsNotNone(focused_layout)
        self.assertEqual(results.findtext("orientation"), "horizontal")
        self.assertGreaterEqual(int(focused_layout.attrib["width"]), int(item_layout.attrib["width"]))
        focus_textures = focused_layout.findall(".//texture")
        self.assertTrue(focus_textures)
        self.assertTrue(any(texture.attrib.get("border") for texture in focus_textures))
        self.assertGreater(len(focused_layout.findall(".//control")), len(item_layout.findall(".//control")))

    def test_xml_navigation_targets_real_controls_for_all_four_remote_directions(self):
        for control_id in DiscoveryWindow.CATEGORY_IDS:
            with self.subTest(control_id=control_id):
                control = self.controls[control_id]
                for direction in ("onleft", "onright", "onup", "ondown"):
                    target = int(control.findtext(direction))
                    self.assertIn(target, self.controls)
        results = self.controls[DiscoveryWindow.RESULTS_ID]
        self.assertIsNotNone(results.findtext("onup"))
        self.assertIsNotNone(results.findtext("ondown"))
        retry = self.controls[DiscoveryWindow.RETRY_ID]
        for direction in ("onleft", "onright", "onup", "ondown"):
            self.assertIn(int(retry.findtext(direction)), self.controls)

    def test_xml_is_static_1280_by_720_with_builtin_assets_and_legible_text(self):
        background = self.controls[DiscoveryWindow.BACKDROP_ID]
        self.assertEqual(int(background.findtext("left")), 0)
        self.assertEqual(int(background.findtext("top")), 0)
        self.assertEqual(int(background.findtext("width")), 1280)
        self.assertEqual(int(background.findtext("height")), 720)
        self.assertIsNone(self.root.find(".//animation"))
        self.assertIsNone(self.root.find(".//effect"))
        for font in self.root.findall(".//font"):
            self.assertTrue((font.text or "").startswith("font"))
        for text_color in self.root.findall(".//textcolor"):
            self.assertIn(text_color.text, ("white", "grey", "gray", "FFCCCCCC", "FFFFFFFF"))
        self.assertEqual(self.controls[DiscoveryWindow.DETAIL_TITLE_ID].findtext("textcolor"), "white")


if __name__ == "__main__":
    unittest.main()
