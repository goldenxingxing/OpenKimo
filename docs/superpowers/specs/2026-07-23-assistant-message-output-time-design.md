# Assistant Message Output Time Design

## Goal

Show when each completed agent text response was output. The relative time
appears after the existing Copy and Fork controls when the user hovers over the
message.

## Time semantics

The displayed timestamp is the completion time of that specific assistant text
message. It is not the session creation time, turn start time, or the current
time when history is replayed.

The backend already persists a timestamp beside every record in `wire.jsonl`.
JSON-RPC event messages will expose an optional event timestamp for both live
delivery and replay. During replay the value comes from the persisted record;
during live delivery it is assigned when the event is sent.

The frontend records the timestamp that completes an assistant text message.
This keeps the value stable across reconnects, page reloads, and application
restarts.

## Presentation

The timestamp is rendered after Copy and Fork inside the existing hover-reveal
message action row. It is shown only for completed assistant text messages.
User messages, streaming text, thinking blocks, tool calls, status messages,
and other assistant variants do not display it.

The visible label uses the user's local time zone:

- less than one minute: `刚刚`
- less than one hour: whole minutes, such as `5分钟前`
- less than one day: whole hours, such as `2小时前`
- less than seven days: whole days, such as `3天前`
- seven days or more: localized calendar date

The `<time>` element carries the ISO timestamp in `dateTime`. Its native title
shows the complete localized date and time, including seconds.

Relative labels refresh at a low frequency while the page remains open so they
do not become stale. The refresh interval does not mutate message data.

## Data model and compatibility

`LiveMessage` gains an optional completion timestamp. It remains optional so
older servers and legacy history files continue to render normally without a
time label.

The JSON-RPC event timestamp is an additive optional field. Existing clients
that ignore unknown fields remain compatible, and the persisted wire message
schema is unchanged.

## Testing

- Backend tests verify that live events receive a timestamp and replayed events
  preserve their persisted record timestamp.
- Frontend unit tests cover all relative-time boundaries and invalid/missing
  timestamps.
- Message reducer tests verify that a completed assistant text message receives
  the completion timestamp during both live delivery and replay.
- Component tests verify that the time follows Copy and Fork and is omitted for
  streaming or non-text messages.
- Existing Web lint, type checking, unit tests, and production build must pass.

