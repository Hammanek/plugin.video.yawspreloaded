"""Side-effect-free Kodi and add-on recorders for deterministic unit tests."""


class KodiRecorder:
    def __init__(self):
        self.calls = []
        self.items = []
        self.end_of_directory_calls = []
        self.resolved_url_calls = []
        self.builtin_commands = []
        self.notifications = []

    def _record(self, method, *args, **kwargs):
        self.calls.append({"method": method, "args": args, "kwargs": kwargs})

    def addDirectoryItem(self, handle, url, listitem, isFolder=False):
        self._record("addDirectoryItem", handle, url, listitem, isFolder)
        self.items.append(
            {"handle": handle, "url": url, "listitem": listitem, "isFolder": isFolder}
        )
        return True

    def endOfDirectory(
        self, handle, succeeded=True, updateListing=False, cacheToDisc=True
    ):
        call = {
            "handle": handle,
            "succeeded": succeeded,
            "updateListing": updateListing,
            "cacheToDisc": cacheToDisc,
        }
        self._record("endOfDirectory", handle, **call)
        self.end_of_directory_calls.append(call)

    def setResolvedUrl(self, handle, succeeded, listitem):
        call = {"handle": handle, "succeeded": succeeded, "listitem": listitem}
        self._record("setResolvedUrl", handle, succeeded, listitem)
        self.resolved_url_calls.append(call)

    def setContent(self, handle, content):
        self._record("setContent", handle, content)

    def setPluginCategory(self, handle, category):
        self._record("setPluginCategory", handle, category)

    def addSortMethod(self, handle, sortMethod):
        self._record("addSortMethod", handle, sortMethod)

    def executeBuiltin(self, command):
        self._record("executeBuiltin", command)
        self.builtin_commands.append(command)

    def notification(self, heading, message, icon=None, time=None, sound=None):
        call = {
            "heading": heading,
            "message": message,
            "icon": icon,
            "time": time,
            "sound": sound,
        }
        self._record("notification", **call)
        self.notifications.append(call)

    def log(self, message, level=None):
        self._record("log", message, level)


class AddonRecorder:
    """Small settings/localization double with no profile filesystem access."""

    def __init__(self, settings=None, strings=None, addon_info=None, language="en"):
        self.settings = dict(settings or {})
        self.strings = dict(strings or {})
        self.addon_info = dict(addon_info or {})
        self.language = language
        self.calls = []

    def getSetting(self, key):
        self.calls.append(("getSetting", key))
        return self.settings.get(key, "")

    def setSetting(self, key, value):
        self.calls.append(("setSetting", key, value))
        self.settings[key] = str(value)

    def getLocalizedString(self, string_id):
        self.calls.append(("getLocalizedString", string_id))
        return self.strings.get(string_id, "")

    def getAddonInfo(self, key):
        self.calls.append(("getAddonInfo", key))
        return self.addon_info.get(key, "")
