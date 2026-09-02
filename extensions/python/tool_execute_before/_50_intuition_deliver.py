from helpers.extension import Extension


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



class IntuitionDeliver(Extension):
    """Non-blocking delivery point before tool execution.

    If the background analysis already finished, deliver the hint right before
    the tool executes. With max_wait_ms > 0 it may wait that tiny amount for an
    already-running task — it never starts a new analysis and never blocks
    beyond the configured wait (cached on the watcher at construction).
    """

    async def execute(self, tool_name="", tool_args={}, **kwargs):
        if not self.agent or not _helper_ok():
            return
        try:
            _helper.start_tool_watchdog(
                agent=self.agent,
                tool_name=str(tool_name),
                tool_args=tool_args if isinstance(tool_args, dict) else {},
            )
        except Exception:
            pass
        try:
            watcher = _helper.get_watcher(self.agent)
            max_wait_ms = watcher.max_wait_ms
        except Exception:
            return
        try:
            await watcher.try_deliver(
                self.agent,
                tool_name=tool_name,
                tool_args=tool_args,
                max_wait_ms=max_wait_ms,
            )
        except Exception:
            pass