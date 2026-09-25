# Gaps: what the plugin cannot do through Hermes' official hooks

keryx-stream 0.4 reproduces the in-tree Keryx side-channel (the private
`gateway/keryx_stream.py` that core-patches a Hermes checkout) using only the
plugin surface. Most of it ports. This file lists what does not, and for each
item the hook Hermes would need to add.

Line references are to hermes-agent `v2026.9.24` (0.21.5).

## How the gaps are filled today

`POST /keryx/publish` accepts any side-channel event from a bearer-authed caller.
A private shim that still patches core can publish the frames below into the
plugin's hub, and the app renders them the same as frames the plugin produced.

The `features` list in `GET /keryx/health` is derived, not hard-coded (see
`keryx_stream/probe.py`). `status` is only listed once a `status` frame has
actually gone out, and `subagents` only once a `tool` frame with `phase: "sub"`
has. A stock install advertises what the hooks can feed, and an install with a
shim advertises more once the shim has fed it. Nothing is listed on the chance
that it might be fed.

## Not portable

### 1. Status rows other than the compaction summary call

The monolith wraps `agent._emit_status` / `agent._emit_warning` and
`agent._compress_context` on each turn's agent. That gives it every lifecycle
line (`kind: "lifecycle"`), every warning (`kind: "warning"`), and one `ready`
when a compression path returns, whether or not a summary model ran.

The plugin sees only the auxiliary LLM call. `pre_auxiliary_call` /
`post_auxiliary_call` fire once per provider attempt of an auxiliary call
(`agent/auxiliary_hooks.py:117-135`, `:202`). With `aux_task == "compression"`
the plugin emits `compacting` and then `ready` (plus a `warning` when the call
errored). This leaves out:

- lifecycle and warning lines (`agent/status_output.py:73`, `:99`, `:103`).
  There is no hook for them, and on chat platforms the gateway swallows the
  routine ones;
- compaction that makes no summary call (pruning-only passes, the codex
  app-server path at `agent/conversation_compression.py:4249`). These produce
  no `compacting`/`ready` pair;
- a single `ready` after a summary call that retries across providers. The
  plugin emits `ready` after each attempt, because the hook fires per attempt
  and not when `_compress_context` returns (`agent/compression_facade.py:221`).

Needed: an observer hook for `_emit_status_kind` (kind, message, origin), plus a
`compression_start` / `compression_end` pair around `_compress_context`.

### 2. Subagent wing detail

`subagent_start` (`tools/delegate_tool.py:292`) and `subagent_stop`
(`tools/delegate_tool_results.py:366`) give the plugin the start and completion
rows. The child's own `pre_tool_call` (matched by child session id) gives it the
child's tool rows. The rest of what the monolith shows only exists on the
parent's `tool_progress_callback` relay (`tools/delegate_tool_progress.py:329`),
which plugins cannot subscribe to:

- `subagent.spawn_requested` (`tools/delegate_tool.py:288`), for a child that
  is queued on a saturated pool;
- `subagent.thinking` (`:381`) and `subagent.progress` (`:344`), which feed the
  wing's activity line;
- the identity and rollup fields on the relay: `model`, `task_index`,
  `task_count`, `depth`, `tool_count`, `input_tokens`, `output_tokens`,
  `reasoning_tokens`, `api_calls`, `files_read`, `files_written`. `subagent_stop`
  carries only role, summary, status, tool history and duration. It also has no
  `child_subagent_id`, so the plugin correlates the stop by child session id.

Needed: a `subagent_progress` observer hook with the relay's
`(event_type, tool_name, preview, **identity)`, and the identity/rollup fields
on `subagent_stop`.

### 3. Tool results after `transform_tool_result`

The monolith's `end` frame carries the result after the `transform_tool_result`
plugins ran, so a verdict another plugin appended (for example a syntax checker
on `write_file`) shows up on the phone. `post_tool_call` fires with the result
before the transform (`model_tools.py:957`; the transform runs at `:959`).
Registering `transform_tool_result` would not help: its results are not chained
(first non-None wins, `model_tools.py:848-864`).

Needed: the final result on `post_tool_call`, or a `post_tool_result` observer
that fires after the transform.

### 4. Matrix edit suppression while the app is watching

The monolith's `suppress_protocol_edits` is called from the stream consumer's
edit decision. When a Keryx subscriber is live it skips the interval
`m.replace` edits, because the side-channel already carries the tokens. When
nobody is watching it falls back to throttled edits. The plugin has no hook in
`GatewayStreamConsumer._should_edit` (`gateway/stream_consumer.py:727`), so the
choice is all or nothing. `/keryx/health` hints
`display.platforms.matrix.streaming: false`, which turns the edits off for good
and so drops the fallback for a Matrix client with no side-channel.

Needed: a per-message delivery hook (or a gateway predicate) that lets a plugin
veto interim edits for a chat.

### 5. Reasoning folded into the streamed Matrix message

With streaming delivery the stream consumer commits the final message itself,
and the gateway skips the normal send, which is the only path that prepends the
💭 reasoning block (`gateway/run_turn.py:4038`). The monolith edits the streamed
message in place to add it (`prepend_reasoning_to_streamed`). A plugin gets
neither the delivered message id nor an adapter handle.

Needed: a post-delivery hook with the message ref and `last_reasoning`, or core
applying `show_reasoning` on the streamed path.

### 6. Early commit on `/steer`

The monolith patches the completion loop so that a pending `/steer` during a
text-only answer ends the stream after at least 120 characters. The partial
answer commits and the steer becomes the next turn. Upstream `redirect()`
(`agent/interrupt_control.py:255`) cancels the request and discards the partial
answer, and `steer()` (`:244`) waits for the answer to finish. No hook runs
inside the stream loop.

Needed: a steer policy hook, or a core option for "commit partial on steer".

## Worked around (no shim needed)

These were on the original gap list. Checked against the tree, each is covered
by something the plugin can reach.

- **`chat_id` absent from the stream payloads.** `_stream_hook_base_payload`
  carries `turn_id`, `iteration`, `session_id`, `model`, `provider`, `surface`
  and no chat id (`agent/stream_delivery.py:270-278`). The plugin borrows the
  session store from `pre_gateway_dispatch` and resolves the session's origin
  chat (`keryx_stream/routing.py`). Limit: a session that has not passed through
  `pre_gateway_dispatch` in this gateway process since start (for example a cron
  delivery) publishes under its session key only.
- **`finish_reason` absent from `on_stream_end`.** That is true
  (`agent/stream_delivery.py:284` passes `final_text`, `finished`, `error`), but
  `post_api_request` carries `finish_reason` (`agent/turn_response_intake.py:61-84`),
  and the end of the turn comes from `post_llm_call` anyway. Dropped as a gap.
- **`reasoning_config` absent from the `llm_request` middleware context.** The
  context is task, turn, request id, session, platform, model, provider,
  base_url, api_mode and call count (`agent/turn_api_request.py:143-148`). The
  request itself carries the effective effort as top-level `reasoning_effort`,
  which Hermes' custom-provider profile writes, and the middleware reads that
  (`keryx_stream/thinking.py`). Limit: a route whose profile emits no
  `reasoning_effort` leaves the dial as it is.
- **Usage frame.** The monolith read `context_compressor.last_prompt_tokens` /
  `context_length` off the live agent. The plugin takes `prompt_tokens` from
  `post_api_request` `usage` and resolves the window from `model.context_length`
  or Hermes' model metadata (`keryx_stream/__init__.py` `_context_length`). On a
  session whose model was switched mid-run, the window can differ from the
  compressor's.
