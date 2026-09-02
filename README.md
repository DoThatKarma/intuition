# Intuition

A quiet inner voice for Agent Zero. A small second brain that watches the agent work and whispers best-practice hints when it sees something dumb or obviously forgotten — **without ever stopping the flow**.

Inspired by the `_infection_check` plugin (same collection/analysis skeleton), but with the opposite contract: where Infection Check gates and can terminate, Intuition only observes and suggests.

## Why You Need It

When you have long interactions with LLMs, they do great work most of the time — but from time to time they just do unexpected things. Even though your prompts are perfect and the model understood everything an hour ago, it still misses things when working on large, complex tasks.

Intuition solves this in a subtle way: it watches the datastream and sends a 💡 into the agent's chat — not as a prompt, but as an Agent Zero warning — which the agent can choose to listen to or simply not care about. (Like real people with their intuition.)

You can also turn it all the way up to MAX and use it more like a **Mentor**: if you don't want to set up a complex multi-agent workflow for a task but still want feedback, you get a second opinion watching over your agent's shoulder — instead of sending everything to a separate reviewer.

And it costs almost nothing: a tiny utility-model call per watched step, in the background, and it never blocks or interrupts anything.

## What It Does

While the agent streams its reasoning and response, a configurable "inner voice" model analyzes the current step in the background. If it spots something clearly dumb — editing a file nobody read, claiming done without verifying, redoing work that already succeeded, a destructive action with no backup, ignoring an explicit user instruction — or if it quietly forgets your original prompt while deep in a long task (the warning can nudge it back on track) — it speaks up. Otherwise it stays silent (~95% of the time).

Hints are delivered as a small log item in the chat and/or as a message injected into the agent's history, so the main model actually reads the tip on its next step. Nothing is ever blocked, nothing is ever terminated.

## How It Works

1. **Collection** — During streaming, the plugin collects the agent's reasoning and response text via `reasoning_stream_chunk` and `response_stream_chunk` extensions.
2. **Analysis** — A background task sends the current step (plus recent history for context) to the configured model with the customizable inner-voice prompt. Sensitivity level and enabled focus areas are appended automatically.
3. **Delivery** — If the verdict is a hint, it is delivered non-blockingly:
   - before the next tool executes (if the analysis already finished, or within `max_wait_ms`),
   - or late, at the start of the next iteration, before the next model call.
4. **Silence** — `<ok/>` verdicts, parse failures, errors, and duplicate hints all result in silence. The flow is never disturbed.

## Never-Blocking Guarantee

- Tool execution is never awaited beyond the optional `max_wait_ms` (default 0 = never wait).
- Analysis runs as a background `asyncio.Task`; a hint that arrives too late is either delivered late or discarded.
- All extensions swallow their own errors. Intuition can never crash the agent, and it never raises `HandledException`.

## Modes

| Mode | What is Analyzed | When Analysis Starts | Latency |
|---|---|---|---|
| **thoughts** (default) | Reasoning + response so far, including the planned tool call | When `headline` or `tool_name` appear in response stream | Low — runs in parallel while the response still streams |
| **complete** | Reasoning + full response | After the entire response stream ends | Higher — full context incl. tool arguments |

## Sensitivity & Focus

| Sensitivity | Speaks when |
|---|---|
| **low** | Blatant mistakes only (≥ 90% confident) |
| **medium** (default) | Obvious issues (≥ 75% confident) |
| **high** | Any notable improvement (≥ 60% confident) |

Focus areas let you choose what the inner voice watches: `best_practices`, `efficiency`, `safer_actions`, `clear_communication`. Unticked areas are not watched. If all areas are unticked, no filter is applied and everything is watched.

## Pacing

- **Focus calls** — how many of the agent's work steps (iterations) the inner voice analyzes per request (default 8, `0` = unlimited). Each watched step costs one small background model call (utility model by default), so this caps watching costs on long tasks. Once the budget is spent, the voice stays quiet for the rest of that request — it never blocks anything — and the budget resets automatically on your next message.
- **Cooldown** — minimum iterations between hints (default 3, 0 = off). Prevents nagging.
- **Dedupe** — an identical hint is never repeated.
- **Hang watchdog** — if a tool call (especially a delegated sub-agent) has produced no result for N minutes (default 10, `0` = off), Intuition actively checks whether it looks stuck and, if so, sends a proper warning to the agent *and* a desktop notification to you — instead of everyone waiting politely forever.
- **Analysis timeout** — max seconds for one background analysis (default 90, `0` = off). A wedged provider connection is cut and its watch slot refunded, so a provider outage can no longer silently burn the whole watch budget and leave the voice deaf for the rest of the request. Failed analyses are also logged (first failure, then every 10th) instead of disappearing in silence.
- **Real Agent Zero Nudge** — optional, off by default. On top of the usual hint, Intuition can also fire a real Agent Zero Nudge — the same interrupting mechanism as the `/nudge` command — telling the agent to continue with the hint in mind. It only fires after a delivered (never suppressed) hint and at most once per 5 minutes, so it cannot turn into a loop. Great for Mentor-style setups.

## Delivery Targets

| Setting | Behavior |
|---|---|
| `agent` | Hint is injected into the agent's history and shown as a small info item in the chat |
| `user` | Hint is shown in the chat log only (never reaches the model) |
| `both` (default) | History injection + visible log item (warning styling) |

With `user` or `both`, every hint also raises a desktop notification (**Intuition - Alert!** — *"The intuition noticed strange behaviour.."* with the hint as the detail), so you see what the inner voice reacted to even when you are not watching the chat. The hang watchdog warning additionally reaches the agent's history in all modes.

## Configuration

| Setting | Default | Description |
|---|---|---|
| Mode | `thoughts` | `thoughts` or `complete` |
| Model | `utility` | `utility` (faster/cheaper) or `main` (more capable) |
| Sensitivity | `medium` | `low`, `medium`, or `high` |
| Focus areas | *(all four)* | What the inner voice watches |
| Focus calls | `8` | Work steps watched per request, then quiet until your next message (0 = unlimited) |
| Cooldown | `3` | Minimum iterations between hints (0 = off) |
| Hang watchdog | `10` | Minutes a tool call may stall before Intuition checks whether it looks stuck (0 = off) |
| Analysis timeout | `90` | Max seconds per background analysis before it is cancelled and the watch slot refunded (0 = off) |
| Delivery | `both` | `agent`, `user`, or `both` |
| Real Agent Zero Nudge | `false` | Also send a real (interrupting) Agent Zero Nudge after a delivered hint (max once per 5 min) |
| Max wait (ms) | `0` | Optional max pause before tool execution; 0 = never wait |
| History size | `10` | Recent messages included as context (0 = entire history) |
| Prompt | *(built-in)* | Fully customizable inner-voice system prompt |

## Key Files

- **Watcher logic**
  - `helpers/intuition.py` implements stream collection, background analysis, budget/cooldown, dedupe, and non-blocking delivery.
- **Extensions**
  - `extensions/python/monologue_start/_10_intuition_reset.py`
  - `extensions/python/reasoning_stream_chunk/_50_intuition_collect.py`
  - `extensions/python/response_stream_chunk/_50_intuition_collect.py`
  - `extensions/python/response_stream/_50_intuition_analyze.py`
  - `extensions/python/response_stream_end/_50_intuition_analyze.py`
  - `extensions/python/tool_execute_before/_50_intuition_deliver.py`
  - `extensions/python/tool_execute_after/_50_intuition_watch_stop.py`
  - `extensions/python/message_loop_start/_50_intuition_deliver_late.py`
  - `extensions/python/monologue_end/_50_intuition_cleanup.py`

## Extension Points Used

| Extension Point | File | Purpose |
|---|---|---|
| `monologue_start` | `_10_intuition_reset.py` | Reset per-request budget/cooldown state |
| `reasoning_stream_chunk` | `_50_intuition_collect.py` | Accumulate reasoning text |
| `response_stream_chunk` | `_50_intuition_collect.py` | Accumulate response text |
| `response_stream` | `_50_intuition_analyze.py` | Detect thoughts complete → start background analysis |
| `response_stream_end` | `_50_intuition_analyze.py` | Start analysis (complete mode / fallback) |
| `tool_execute_before` | `_50_intuition_deliver.py` | Non-blocking delivery before tool execution |
| `tool_execute_after` | `_50_intuition_watch_stop.py` | Disarms the hang watchdog when the tool call returns |
| `message_loop_start` | `_50_intuition_deliver_late.py` | Late delivery before the next model call |
| `monologue_end` | `_50_intuition_cleanup.py` | Cancel stale analysis at request end |

## Configuration Scope

- **Settings section**: `agent`
- **Per-project config**: `true`
- **Per-agent config**: `true`

## Plugin Metadata

- **Name**: `intuition`
- **Title**: `Intuition`
- **Version**: `0.3.2`
- **Description**: A quiet inner voice that watches the agent work and whispers best-practice hints when it sees something dumb or obviously forgotten — never blocks the flow.

## Assets

- `webui/thumbnail.jpg` — plugin list icon (rendered from `webui/intuition.svg`, the editable design source).

## Credits

- **Intuition** was created by **DoThat from Nexum AI - [nxm.nu](https://nxm.nu)** — and yes, it was built *with* Intuition: Agent Zero as the senior developer, and DoThat's Intuition 💡 quietly watching over the whole build.
- **Architecture inspiration**: the built-in [`_infection_check`](https://github.com/agent0ai/agent-zero/tree/main/plugins/_infection_check) plugin by the Agent Zero team. Intuition reuses its collection/analysis/gating skeleton with the opposite contract — where Infection Check protects the *user* by blocking, Intuition helps the *agent* with a quiet hint. Thank you for excellent work. We love it!
