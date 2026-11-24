"""Route parsed plugin parameters through exactly one completion adapter."""

try:
    from urllib.parse import parse_qsl
except ImportError:  # pragma: no cover - retained for older Kodi interpreters
    from urlparse import parse_qsl

from yawsp_app.runtime import CompletionKind, completion_for


def dispatch(paramstring, handlers, runner):
    """Dispatch with the legacy query semantics, including unknown-action menus."""
    params = dict(parse_qsl(paramstring))
    action = params.get("action") if params else None
    selected = completion_for(action, params)
    handler = handlers.get(action) if action else None
    if handler is None:
        handler = handlers["menu"]

    if selected is CompletionKind.PLAYBACK:
        return runner.run_play(lambda: handler(params))
    if selected is CompletionKind.ACTION:
        return runner.run_action(lambda: handler(params))
    return runner.run_directory(lambda: handler(params))
