from helpers.extension import Extension
from agent import LoopData


def _load_helper():
    """Import the plugin helper, self-healing a stale cached module.

    If the plugin is updated while the framework process is running, an old
    helper module can remain cached in sys.modules and break this import. In
    that case the cached module is purged once and re-imported from disk.
    """
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


class IntuitionCleanup(Extension):
    """Request finished: cancel any analyses that are still running."""

    async def execute(self, loop_data=LoopData(), **kwargs):
        if not self.agent or _helper is None:
            return
        try:
            _helper.request_finished(self.agent)
        except Exception:
            pass
