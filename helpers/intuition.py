import re
import asyncio
from typing import TYPE_CHECKING

from helpers import plugins
from helpers import history as history_helpers
from langchain_core.messages import HumanMessage, SystemMessage

if TYPE_CHECKING:
    from agent import Agent

PLUGIN_NAME = "intuition"
DATA_KEY_WATCHER = f"_plugin.{PLUGIN_NAME}.watcher"
DATA_KEY_PENDING = f"_plugin.{PLUGIN_NAME}.pending_watchers"
DATA_KEY_BUDGET = f"_plugin.{PLUGIN_NAME}.budget_used"
DATA_KEY_LAST_NUDGE = f"_plugin.{PLUGIN_NAME}.last_nudge_iter"
DATA_KEY_LAST_REAL_NUDGE = f"_plugin.{PLUGIN_NAME}.last_real_nudge_ts"
DATA_KEY_LAST_HINT = f"_plugin.{PLUGIN_NAME}.last_hint"
DATA_KEY_RECENT_HINTS = f"_plugin.{PLUGIN_NAME}.recent_hints"
DATA_KEY_REQUEST_SIG = f"_plugin.{PLUGIN_NAME}.request_sig"
DATA_KEY_REQUEST_OPEN = f"_plugin.{PLUGIN_NAME}.request_open"
DATA_KEY_TOOLWATCH = f"_plugin.{PLUGIN_NAME}.tool_watch_task"
DATA_KEY_ITER_SEEN = f"_plugin.{PLUGIN_NAME}.iter_seen"
DATA_KEY_ERR_STREAK = f"_plugin.{PLUGIN_NAME}.err_streak"

HINT_HEADING = "💡 Intuition"
HINT_PREFIX = "💡 Intuition:"

# Bumped whenever helper code changes: extensions check this marker so a
# stale cached module in a long-running process is detected, purged and
# re-imported from disk (self-healing live updates).
RUNTIME_MARKER = "v12-reliability"

_RE_OK = re.compile(r"<ok\s*/>")
_RE_NUDGE = re.compile(r"<nudge>(.*?)</nudge>", re.DOTALL)

SENSITIVITY_TEXTS = {
    "low": (
        "Sensitivity level: LOW. Speak only on blatant, unambiguous mistakes — "
        "you must be at least 90% confident the issue is real and worth speaking up "
        "about. When in any doubt, output <ok/>."
    ),
    "medium": (
        "Sensitivity level: MEDIUM. Speak on obvious issues you are at least 75% "
        "confident about. When in doubt, output <ok/>."
    ),
    "high": (
        "Sensitivity level: HIGH. Speak on any notable improvement you are at least "
        "60% confident about — but still prefer silence whenever unsure."
    ),
}

FOCUS_AREA_TEXTS = {
    "best_practices": (
        "best_practices — verifying before claiming done; reading files before "
        "editing them; reusing existing work instead of rewriting it; testing; "
        "leaving no mess behind"
    ),
    "efficiency": (
        "efficiency — redundant or wasteful work, repeating something that just "
        "succeeded, ignoring existing solutions, overcomplicated detours"
    ),
    "safer_actions": (
        "safer_actions — destructive or irreversible actions without backup or "
        "confirmation, guessing instead of checking, ignoring errors"
    ),
    "clear_communication": (
        "clear_communication — drifting from the user's actual request, ignoring "
        "visible instructions or constraints, overcomplicated answers"
    ),
}

FALLBACK_PROMPT = (
    "You are Intuition — an experienced senior developer quietly watching another "
    "AI agent work. Analyze the agent's current output for obvious mistakes or "
    "forgotten basics and stay silent unless something is clearly wrong. "
    "The text often describes what the agent is ABOUT to do: judge the plan "
    "itself (is running this a good idea?), never assume it already ran. "
    "Never nudge about JSON formatting, cut-off output or protocol errors: "
    "the framework automatically repairs and retries those, so such hints "
    "cannot help the agent. "
    "Reply with 1-3 sentences of analysis, then output your verdict as the VERY "
    "LAST thing: either <ok/> (nothing worth saying) or "
    "<nudge>one short actionable hint, max 2 sentences</nudge>."
)


def get_config(agent: "Agent") -> dict:
    return plugins.get_plugin_config(PLUGIN_NAME, agent=agent) or {}


def parse_result(text: str) -> tuple[str, str]:
    """Find the *last* occurrence of any verdict tag in *text*."""
    pos = -1
    action, detail = "ok", ""
    for m in _RE_OK.finditer(text):
        if m.start() > pos:
            pos, action, detail = m.start(), "ok", ""
    for m in _RE_NUDGE.finditer(text):
        if m.start() > pos:
            pos, action, detail = m.start(), "nudge", m.group(1).strip()
    return action, detail


def _read_result(task: asyncio.Task) -> tuple[str, str, str] | None:
    """Safely read a finished analysis task's result.

    CancelledError is a BaseException on Python 3.12+ and would pierce every
    'except Exception' guard up to the agent loop, so it must never escape.
    """
    if task.cancelled():
        return None
    try:
        return task.result()
    except BaseException:  # noqa: BLE001 - includes CancelledError, deliberately
        return None


def _harvest(agent: "Agent", old: "IntuitionWatcher | None"):
    """Preserve a previous watcher's analysis before it gets replaced.

    Finished and undelivered nudges are delivered immediately; still-running
    analyses are parked so late delivery or cleanup can handle them later.
    """
    try:
        if old is None or old._delivered or old._task is None:
            return
        if old._task.done():
            result = _read_result(old._task)
            if result is not None and result[0] == "nudge" and result[1]:
                old._deliver(agent, result[1])
            return
        pending = agent.get_data(DATA_KEY_PENDING)
        if not isinstance(pending, list):
            pending = []
        if old not in pending:
            pending.append(old)
        # v0.3.2 (m3): cap parked watchers - a persistently wedged provider
        # would otherwise grow this list for the whole request.
        while len(pending) > 8:
            dropped = pending.pop(0)
            try:
                if dropped is not None:
                    dropped.cancel_stale()
            except Exception:
                pass
        agent.set_data(DATA_KEY_PENDING, pending)
    except Exception:
        pass


HANG_SYSTEM_PROMPT = (
    "You are the Intuition hang watchdog watching an AI agent. It made a "
    "tool call that has produced no result for a long time. Output <ok/> "
    "if the tool is plausibly legitimately long-running (large install, "
    "big test suite, training, big download, long build). Otherwise output "
    "<nudge>message</nudge>: one short concrete message (max 3 sentences) "
    "saying what looks stuck, that it has produced nothing for many minutes, "
    "and that it should be checked, poked or cancelled. Prefer alerting when "
    "the stuck call delegated work to a subordinate agent that has delivered "
    "nothing."
)


def start_tool_watchdog(agent: "Agent", tool_name: str, tool_args: dict):
    """Arm the inactivity watchdog for a starting tool call."""
    try:
        timeout_min = float(get_config(agent).get("hang_timeout", 10) or 0)
        if timeout_min <= 0:
            return
        stop_tool_watchdog(agent)  # never two loops at once
        preview = str(tool_args)[:400]
        task = asyncio.create_task(
            _tool_watch_loop(agent, str(tool_name), preview, timeout_min)
        )
        agent.set_data(DATA_KEY_TOOLWATCH, task)
    except Exception:
        pass


def stop_tool_watchdog(agent: "Agent"):
    """Disarm the watchdog (tool returned; cancel is a no-op if done)."""
    try:
        task = agent.get_data(DATA_KEY_TOOLWATCH)
        if task is not None and not task.done():
            task.cancel()
        agent.set_data(DATA_KEY_TOOLWATCH, None)
    except Exception:
        pass


async def _tool_watch_loop(
    agent: "Agent", tool_name: str, preview: str, timeout_min: float
):
    """Fire once after timeout_min without a tool result, then stay quiet."""
    try:
        await asyncio.sleep(float(timeout_min) * 60.0)
        try:
            hist = agent.history.output()[-10:]
            hist_text = history_helpers.output_text(
                hist, ai_label="assistant", human_label="user"
            )
        except Exception:
            hist_text = "(unavailable)"
        payload = (
            f"The tool call below started {timeout_min:g} minutes ago and "
            f"has produced no result since.\n\nTool: {tool_name}\n"
            f"Arguments (excerpt): {preview}\n\n"
            f"## Recent conversation\n{hist_text}"
        )
        watcher = IntuitionWatcher(config=get_config(agent), iteration=-999)
        watcher._no_anchor = True  # v0.3.2 (M5): keep -999 out of the anchor
        msgs = [
            SystemMessage(content=HANG_SYSTEM_PROMPT),
            HumanMessage(content=payload),
        ]
        try:
            model = watcher._get_model(agent)
            # v0.3.2 (M3): same deadline as regular analyses.
            call = model.unified_call(messages=msgs)
            if watcher.analysis_timeout > 0:
                async with asyncio.timeout(watcher.analysis_timeout):
                    response, _ = await call
            else:
                response, _ = await call
        except Exception:
            return
        action, hint = parse_result(response)
        if action == "nudge" and hint:
            watcher._deliver(agent, hint)
    except asyncio.CancelledError:
        raise
    except Exception:
        return


def reset_request_state(agent: "Agent"):
    """Reset per-request state. Called on monologue_start (new user request)."""
    sig = _request_signature(agent)
    if (
        sig
        and bool(agent.get_data(DATA_KEY_REQUEST_OPEN))
        and sig == agent.get_data(DATA_KEY_REQUEST_SIG)
    ):
        # Framework-internal retry of the SAME request (the outer monologue
        # loop re-entered after an exception): keep budget/cooldown/dedupe
        # state, only clear stale tasks and the dead watcher from the
        # previous attempt.
        cancel_all(agent)
        stop_tool_watchdog(agent)
        agent.set_data(DATA_KEY_WATCHER, None)
        # v0.3.2 (M2): the framework rebuilt LoopData, so iteration restarts
        # at 1 while the cooldown anchor still holds the previous epoch's
        # high value - a late hint could then block every check with a
        # permanently negative delta. Rebase the anchor into the new epoch.
        last_nudge = agent.get_data(DATA_KEY_LAST_NUDGE)
        if last_nudge is not None:
            prior_seen = int(agent.get_data(DATA_KEY_ITER_SEEN) or 0)
            agent.set_data(DATA_KEY_LAST_NUDGE, int(last_nudge) - prior_seen)
        agent.set_data(DATA_KEY_REQUEST_OPEN, True)
        return
    cancel_all(agent)
    stop_tool_watchdog(agent)
    agent.set_data(DATA_KEY_WATCHER, None)
    agent.set_data(DATA_KEY_BUDGET, 0)
    agent.set_data(DATA_KEY_LAST_NUDGE, None)
    agent.set_data(DATA_KEY_ITER_SEEN, 0)
    agent.set_data(DATA_KEY_ERR_STREAK, 0)
    agent.set_data(DATA_KEY_LAST_HINT, "")
    agent.set_data(DATA_KEY_RECENT_HINTS, [])
    agent.set_data(DATA_KEY_REQUEST_SIG, sig)
    agent.set_data(DATA_KEY_REQUEST_OPEN, True)


def _request_signature(agent: "Agent") -> str:
    """Stable signature of the current request: the user's message content.

    Uses agent.last_user_message (captured by the framework at request
    submission) rather than scanning history, because nudges and user
    interventions are also stored as non-ai history entries and would
    otherwise pollute the signature.
    """
    try:
        msg = getattr(agent, "last_user_message", None)
        content = getattr(msg, "content", None)
        if content:
            return str(content)[:512]
    except Exception:
        pass
    try:
        for entry in reversed(agent.history.output()):
            if isinstance(entry, dict) and not entry.get("ai", False):
                return str(entry.get("content", ""))[:512]
    except Exception:
        pass
    return ""


def request_finished(agent: "Agent"):
    """Request is done (monologue_end): clean tasks and close the request."""
    cancel_all(agent)
    # v0.3.2 (M5): a request ending while a tool is still outstanding (user
    # stop, kill, exception mid-tool) must not leave the hang watchdog armed
    # - it could otherwise fire a stale warning many minutes later.
    stop_tool_watchdog(agent)
    try:
        agent.set_data(DATA_KEY_REQUEST_OPEN, False)
    except Exception:
        pass


def cancel_all(agent: "Agent"):
    """Cancel any still-running analyses (current watcher and parked ones)."""
    try:
        watcher = agent.get_data(DATA_KEY_WATCHER)
        if watcher is not None:
            watcher.cancel_stale()
    except Exception:
        pass
    try:
        pending = agent.get_data(DATA_KEY_PENDING)
        if isinstance(pending, list):
            for w in pending:
                try:
                    if w is not None:
                        w.cancel_stale()
                except Exception:
                    pass
    except Exception:
        pass
    agent.set_data(DATA_KEY_PENDING, [])


def get_watcher(agent: "Agent") -> "IntuitionWatcher":
    """Get or create the watcher for the current iteration."""
    loop = getattr(agent, "loop_data", None)
    iteration = loop.iteration if loop else -1
    # v0.3.2 (M2): track the highest iteration seen this request so the
    # retry rebase in reset_request_state can translate the old anchor.
    if iteration >= 0:
        try:
            seen = int(agent.get_data(DATA_KEY_ITER_SEEN) or 0)
            if iteration > seen:
                agent.set_data(DATA_KEY_ITER_SEEN, iteration)
        except Exception:
            pass
    watcher: "IntuitionWatcher | None" = agent.get_data(DATA_KEY_WATCHER)
    if watcher is None or watcher.iteration != iteration:
        _harvest(agent, watcher)  # deliver or park the previous watcher's work
        watcher = IntuitionWatcher(config=get_config(agent), iteration=iteration)
        agent.set_data(DATA_KEY_WATCHER, watcher)
    return watcher


async def deliver_pending(agent: "Agent"):
    """Late-delivery sweep before the next model call.

    Covers the previous iteration's watcher plus any parked watchers whose
    analyses may have finished in the meantime.
    """
    try:
        current = agent.get_data(DATA_KEY_WATCHER)
        if current is not None:
            await current.deliver_late(agent)
    except Exception:
        pass
    try:
        pending = agent.get_data(DATA_KEY_PENDING)
        still_pending: list = []
        if isinstance(pending, list):
            for w in pending:
                try:
                    if w is None or w._delivered or w._task is None:
                        continue
                    if w._task.done():
                        result = _read_result(w._task)
                        if result is not None and result[0] == "nudge" and result[1]:
                            w._deliver(agent, result[1])
                        continue
                    still_pending.append(w)  # still running — keep parked
                except Exception:
                    continue
        agent.set_data(DATA_KEY_PENDING, still_pending)
    except Exception:
        pass


def _norm_hint(text: str) -> str:
    """Normalize a hint for fuzzy dedupe (case/punctuation-insensitive)."""
    lowered = re.sub(r"[^\wæøåÆØÅ\s]", " ", str(text).lower())
    return re.sub(r"\s+", " ", lowered).strip()


def _note_analysis_error(agent: "Agent", exc: Exception):
    """v0.3.2 (M1): failed analyses must be cheap and observable.

    Refunds the watch-budget slot the analysis consumed and logs the first
    error of a streak (then every 10th) so a provider outage becomes visible
    without turning quiet failure into log spam.
    """
    try:
        used = int(agent.get_data(DATA_KEY_BUDGET) or 0)
        if used > 0:
            agent.set_data(DATA_KEY_BUDGET, used - 1)
    except Exception:
        pass
    try:
        streak = int(agent.get_data(DATA_KEY_ERR_STREAK) or 0) + 1
        agent.set_data(DATA_KEY_ERR_STREAK, streak)
        if streak == 1 or streak % 10 == 0:
            agent.context.log.log(
                type="info",
                heading=HINT_HEADING,
                content=(
                    f"(watch) analysis failed ({type(exc).__name__}) - watch "
                    f"slot refunded; consecutive failures: {streak}"
                ),
            )
    except Exception:
        pass


class IntuitionWatcher:
    """Collects streams, analyzes in background, delivers nudges. Never blocks."""

    def __init__(self, config: dict, iteration: int):
        self.mode: str = config.get("mode", "thoughts")
        self.model_choice: str = config.get("model", "utility")
        self.prompt: str = str(config.get("prompt", "") or "").strip() or FALLBACK_PROMPT
        self.sensitivity: str = str(config.get("sensitivity", "medium") or "medium")
        self.focus_areas: list = config.get("focus_areas") or []
        self.focus_calls: int = int(config.get("focus_calls", 8) or 0)
        self.cooldown: int = int(config.get("cooldown", 3) or 0)
        self.history_size: int = int(config.get("history_size", 10) or 0)
        # Cached at construction: the delivery extension and _deliver must
        # not re-read config from disk on every tool call / nudge.
        self.max_wait_ms: int = int(config.get("max_wait_ms", 0) or 0)
        # v0.3.2 (M3): total deadline for one analysis call - a wedged
        # provider connection must free the slot, not park the watcher.
        # 0 disables the deadline.
        self.analysis_timeout: float = float(config.get("analysis_timeout", 90) or 0)
        self.delivery: str = str(config.get("delivery", "both") or "both")
        # v0.3.1: optional real Agent Zero Nudge (interrupting framework
        # mechanism) on top of the whisper. Off by default.
        self.real_nudge: bool = bool(config.get("real_nudge", False))
        self.iteration = iteration

        self.reasoning_log = ""
        self.response_log = ""
        self._task: asyncio.Task | None = None
        self._delivered = False
        # v0.3.2 (M5): watchdog deliveries run outside the iteration
        # economy and must not write their -999 into the cooldown anchor.
        self._no_anchor = False

    # -- collection ----------------------------------------------------------

    def collect_reasoning(self, full_text: str):
        # Frozen once analysis started, mirroring collect_response: the
        # analysis snapshot is prebuilt at trigger time.
        if self._task is None:
            self.reasoning_log = full_text

    def collect_response(self, full_text: str):
        # Stop collecting once background analysis has started
        if self._task is None:
            self.response_log = full_text

    # -- analysis trigger ----------------------------------------------------

    def start_analysis(self, agent: "Agent"):
        """Fire-and-forget background check (called from stream extensions)."""
        if self._task is not None:
            return
        try:
            if self.focus_calls > 0:
                used = int(agent.get_data(DATA_KEY_BUDGET) or 0)
                if used >= self.focus_calls:
                    return  # per-request watching budget exhausted

            last_nudge = agent.get_data(DATA_KEY_LAST_NUDGE)
            if self.cooldown > 0 and last_nudge is not None:
                if (self.iteration - int(last_nudge)) < self.cooldown:
                    return  # still cooling down from the previous nudge

            snapshot = self._build_log()
            if not snapshot.strip():
                return

            # Create the task BEFORE consuming budget: if task creation fails,
            # the watch slot is not lost.
            self._task = asyncio.create_task(self._run_check(agent, snapshot))
            if self.focus_calls > 0:
                used = int(agent.get_data(DATA_KEY_BUDGET) or 0)
                agent.set_data(DATA_KEY_BUDGET, used + 1)
        except Exception:
            self._task = None  # analysis is best-effort, never fatal

    # -- delivery ------------------------------------------------------------

    async def try_deliver(
        self,
        agent: "Agent",
        tool_name: str = "",
        tool_args: dict | None = None,
        max_wait_ms: int = 0,
    ):
        """Deliver the hint if analysis already finished. Optionally wait a little.

        Never blocks tool execution beyond max_wait_ms and never re-runs analysis.
        """
        task = self._task
        if task is None or self._delivered:
            return

        if not task.done():
            if max_wait_ms <= 0:
                return
            try:
                # Shield the analysis task so the timeout does not cancel it —
                # it may still finish and be delivered late. CancelledError is
                # deliberately NOT caught here: cancelling the agent loop (user
                # stop/kill) must always propagate.
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=max_wait_ms / 1000.0
                )
            except asyncio.TimeoutError:
                return

        if not task.done():
            return
        result = _read_result(task)
        if result is not None and result[0] == "nudge" and result[1]:
            self._deliver(agent, result[1])

    async def deliver_late(self, agent: "Agent"):
        """Deliver a finished but undelivered hint before the next model call."""
        await self.try_deliver(agent)

    def cancel_stale(self):
        """Cancel a still-running analysis (hint would arrive too late)."""
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def _deliver(self, agent: "Agent", hint: str):
        self._delivered = True
        hint = hint.strip()
        if not hint:
            return

        try:
            # Fuzzy dedupe: suppress repeats of the last few hints, including
            # paraphrases that only differ in quoted fragments. A suppressed
            # repeat still advances the cooldown anchor so it cannot burn
            # watch budget.
            recent = agent.get_data(DATA_KEY_RECENT_HINTS)
            if not isinstance(recent, list):
                recent = []
            norm = _norm_hint(hint)
            duplicate = any(_norm_hint(h) == norm for h in recent)
            if not duplicate:
                recent.append(hint)
                agent.set_data(DATA_KEY_RECENT_HINTS, recent[-5:])
            agent.set_data(DATA_KEY_LAST_HINT, hint)
            if not self._no_anchor:
                agent.set_data(DATA_KEY_LAST_NUDGE, self.iteration)
            if duplicate:
                return
        except Exception:
            pass

        try:
            # Cached on the watcher at construction — no per-nudge disk read.
            delivery = self.delivery
        except Exception:
            delivery = "both"

        if delivery in ("agent", "both"):
            try:
                agent.hist_add_warning(f"{HINT_PREFIX} {hint}")
            except Exception:
                pass

        # Every fired nudge is always visible in the UI log, regardless of mode
        # (info in agent/user mode, warning when it goes to both channels).
        try:
            agent.context.log.log(
                type="warning" if delivery == "both" else "info",
                heading=HINT_HEADING,
                content=hint,
            )
        except Exception:
            pass

        # Desktop notification (same visibility as infection check alerts).
        if delivery in ("user", "both"):
            try:
                from helpers.notification import (
                    NotificationManager,
                    NotificationPriority,
                    NotificationType,
                )

                NotificationManager.send_notification(
                    type=(
                        NotificationType.WARNING
                        if delivery == "both"
                        else NotificationType.INFO
                    ),
                    priority=NotificationPriority.NORMAL,
                    title="Intuition - Alert!",
                    message="The intuition noticed strange behaviour..",
                    detail=hint,
                    display_time=5,
                )
            except Exception:
                pass

        # v0.3.1: optional real Agent Zero Nudge on top of the whisper.
        # context.nudge() kills the running agent task and injects
        # fw.msg_nudge.md ("Nudged - continue") as a user message, so it must
        # run only after the hint is fully delivered, only when the agent
        # actually received it (agent/both), never for suppressed duplicates,
        # and at most once per 5 minutes to avoid nudge loops.
        if getattr(self, "real_nudge", False) and delivery in ("agent", "both"):
            try:
                import time as _time

                now = _time.time()
                last = agent.get_data(DATA_KEY_LAST_REAL_NUDGE)
                if not isinstance(last, (int, float)) or now - float(last) >= 300:
                    agent.set_data(DATA_KEY_LAST_REAL_NUDGE, now)
                    ctx = agent.context

                    async def _fire_real_nudge() -> None:
                        try:
                            ctx.nudge()
                        except Exception:
                            pass

                    asyncio.create_task(_fire_real_nudge())
            except Exception:
                pass

    # -- internals -----------------------------------------------------------

    def _build_log(self) -> str:
        parts: list[str] = []
        if self.reasoning_log:
            parts.append(f"## Agent Reasoning\n{self.reasoning_log}")
        if self.response_log:
            parts.append(f"## Agent Response\n{self.response_log}")
        return "\n\n".join(parts)

    def _get_model(self, agent: "Agent"):
        if self.model_choice == "main":
            return agent.get_chat_model()
        return agent.get_utility_model()

    def _system_prompt(self) -> str:
        parts: list[str] = [self.prompt]
        parts.append(
            SENSITIVITY_TEXTS.get(self.sensitivity, SENSITIVITY_TEXTS["medium"])
        )
        if self.focus_areas:
            lines = [
                FOCUS_AREA_TEXTS[a]
                for a in self.focus_areas
                if a in FOCUS_AREA_TEXTS
            ]
            if lines:
                parts.append(
                    "Focus areas (only nudge about these; ignore everything else):\n- "
                    + "\n- ".join(lines)
                )
            else:
                parts.append("Focus areas: all areas are enabled (no filter).")
        else:
            parts.append("Focus areas: all areas are enabled (no filter).")
        return "\n\n".join(parts)

    async def _run_check(self, agent: "Agent", log_text: str) -> tuple[str, str, str]:
        try:
            hist = agent.history.output()
            if self.history_size > 0:
                hist = hist[-self.history_size :]

            filtered: list = []
            for entry in hist:
                content = str(entry.get("content", "")) if isinstance(entry, dict) else ""
                if "[BLOCKED]" in content:
                    if filtered:
                        filtered.pop()  # also remove the user message before it
                    continue
                filtered.append(entry)

            hist_text = history_helpers.output_text(
                filtered, ai_label="assistant", human_label="user"
            )
            recent = agent.get_data(DATA_KEY_RECENT_HINTS)
            recent_section = ""
            if isinstance(recent, list) and recent:
                listed = "\n".join(f"- {h}" for h in recent[-5:])
                recent_section = (
                    f"\n\n## Hints already given this request\n{listed}\n"
                    "Do not repeat or paraphrase these. If nothing NEW beyond "
                    "them is visible in the output above, output <ok/> only."
                )
            user_msg = (
                f"## Recent Conversation History\n{hist_text}\n\n"
                f"## Current Agent Output to Analyze\n{log_text}"
                f"{recent_section}"
            )
            msgs = [
                SystemMessage(content=self._system_prompt()),
                HumanMessage(content=user_msg),
            ]

            model = self._get_model(agent)
            # v0.3.2 (M3): hard deadline around the provider call.
            call = model.unified_call(messages=msgs)
            if self.analysis_timeout > 0:
                async with asyncio.timeout(self.analysis_timeout):
                    response, _ = await call
            else:
                response, _ = await call
            action, hint = parse_result(response)
            try:
                agent.set_data(DATA_KEY_ERR_STREAK, 0)
            except Exception:
                pass
            return action, hint, response
        except asyncio.CancelledError:
            # v0.3.2 (M1): cancellation is cleanup/user-stop - it must not
            # be refunded, converted or swallowed.
            raise
        except Exception as exc:
            # v0.3.2 (M1): a failed analysis must not consume a budget slot
            # and must be observable. "error" behaves like "ok" everywhere.
            _note_analysis_error(agent, exc)
            return "error", "", ""
