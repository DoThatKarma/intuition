from helpers.extension import Extension
from agent import LoopData


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


try:
    from helpers import extract_tools
except Exception:  # noqa: BLE001
    extract_tools = None


class IntuitionAnalyzeThoughts(Extension):
    """Thoughts mode: start analysis once the planned tool call is complete."""

    async def execute(self, loop_data=LoopData(), text="", parsed={}, **kwargs):
        if not self.agent or not parsed or not _helper_ok():
            return
        try:
            watcher = _helper.get_watcher(self.agent)
            if watcher.mode != "thoughts":
                return
            # Start ONLY when the planned tool request is fully streamed.
            # Starting earlier once froze a mid-stream prefix as the snapshot,
            # which looks like "truncated JSON" to the analyzer even though
            # the agent did nothing wrong. Turns without a complete tool
            # request are owned by the response_stream_end extension, which
            # starts the analysis with the full response as snapshot.
            if extract_tools is None:
                return
            if extract_tools.extract_tool_request(text) is None:
                return
            watcher.start_analysis(self.agent)
        except Exception:
            pass
