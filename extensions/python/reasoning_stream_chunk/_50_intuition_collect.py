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



class IntuitionCollectReasoning(Extension):
    """Accumulate the agent's reasoning text while it streams."""

    async def execute(self, loop_data=LoopData(), stream_data=None, **kwargs):
        if not self.agent or stream_data is None or not _helper_ok():
            return
        try:
            _helper.get_watcher(self.agent).collect_reasoning(
                stream_data.get("full", "")
            )
        except Exception:
            pass
