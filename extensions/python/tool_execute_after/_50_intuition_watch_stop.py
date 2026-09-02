from helpers.extension import Extension


def _load_helper():
    """Import the plugin helper, self-healing a stale cached module."""
    import importlib
    import sys

    mod_name = "usr.plugins.intuition.helpers.intuition"
    try:
        helper = importlib.import_module(mod_name)
        if getattr(helper, "RUNTIME_MARKER", None) != "v12-reliability":
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


def _helper_ok() -> bool:
    """v0.3.2 (M4): lazy re-import for the helper.

    A one-off import failure at module load (partial plugin write during
    boot, transient fs error) used to disable this extension until process
    restart. Retry the load once per call instead - the marker check inside
    _load_helper still heals stale caches as before.
    """
    global _helper
    if _helper is not None:
        return True
    try:
        _helper = _load_helper()
    except Exception:  # noqa: BLE001 - never break the framework
        return False
    return True



class IntuitionWatchStop(Extension):
    """Disarm the hang watchdog when the tool call returns."""

    async def execute(self, **kwargs):
        if not self.agent or not _helper_ok():
            return
        try:
            _helper.stop_tool_watchdog(self.agent)
        except Exception:
            pass
