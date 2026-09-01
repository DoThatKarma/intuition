from helpers.extension import Extension


def _load_helper():
    """Import the plugin helper, self-healing a stale cached module."""
    import importlib
    import sys

    mod_name = "usr.plugins.intuition.helpers.intuition"
    try:
        helper = importlib.import_module(mod_name)
        if getattr(helper, "RUNTIME_MARKER", None) != "v10-alert":
            raise AttributeError("stale helper cache")
        return helper
    except (ImportError, AttributeError):
        for name in [
            n
            for n in list(sys.modules)
            if n == mod_name or n.startswith(mod_name + ".")
        ]:
            del sys.modules[name]
        importlib.invalidate_caches()
        return importlib.import_module(mod_name)


try:
    _helper = _load_helper()
except Exception:  # noqa: BLE001 - this plugin must never break the framework
    _helper = None


class IntuitionWatchStop(Extension):
    """Disarm the hang watchdog when the tool call returns."""

    async def execute(self, **kwargs):
        if not self.agent or _helper is None:
            return
        try:
            _helper.stop_tool_watchdog(self.agent)
        except Exception:
            pass
