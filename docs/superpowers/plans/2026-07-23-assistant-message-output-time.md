# Assistant Message Output Time Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Display a stable relative completion time after the Copy and Fork controls for each completed assistant text message.

**Architecture:** Add an optional Unix-seconds timestamp to outbound JSON-RPC event messages, using a send-time default for live events and the persisted `wire.jsonl` record timestamp during replay. Convert that value to epoch milliseconds in the Web client, retain the latest text-fragment time on `LiveMessage.completedAt`, and render it through a focused relative-time component in the existing hover action row.

**Tech Stack:** Python 3.12, Pydantic, pytest, TypeScript, React 19, Node test runner, React server rendering, Biome, and Vite.

## Global Constraints

- The displayed time is the completion time of the specific assistant text message.
- Refreshing, reconnecting, or replaying history must not reset old messages to `刚刚`.
- The time appears after Copy and Fork in the existing hover-reveal action row.
- Only completed assistant text messages show a time.
- Custom and legacy servers without event timestamps remain compatible; missing timestamps render no label.
- Relative labels use the browser's local time zone and refresh without mutating message data.

---

## File Structure

- Modify `kimi-cli/src/kimi_cli/wire/jsonrpc.py`: add the optional event timestamp and live default.
- Modify `kimi-cli/src/kimi_cli/wire/server.py`: pass persisted timestamps into replayed events.
- Modify `kimi-cli/tests/core/test_wire_message.py`: cover live serialization and explicit replay timestamps.
- Modify `kimi-cli/web/src/hooks/wireTypes.ts`: declare the optional JSON-RPC event timestamp.
- Modify `kimi-cli/web/src/hooks/types.ts`: add optional `completedAt` epoch milliseconds to assistant messages.
- Create `kimi-cli/web/src/features/chat/message-output-time.ts`: relative-time formatting and the small `<time>` component.
- Create `kimi-cli/web/src/features/chat/message-output-time.test.ts`: boundary and rendered-markup tests.
- Create `kimi-cli/web/src/hooks/message-output-time.ts`: pure message timestamp update helper.
- Create `kimi-cli/web/src/hooks/message-output-time.test.ts`: message data update tests.
- Modify `kimi-cli/web/src/hooks/useSessionStream.ts`: propagate each text event's timestamp into the message.
- Modify `kimi-cli/web/src/features/chat/components/virtualized-message-list.tsx`: render time after Copy and Fork.

### Task 1: Carry persisted timestamps through the wire protocol

**Files:**
- Modify: `kimi-cli/src/kimi_cli/wire/jsonrpc.py`
- Modify: `kimi-cli/src/kimi_cli/wire/server.py`
- Test: `kimi-cli/tests/core/test_wire_message.py`

**Interfaces:**
- Consumes: `WireMessageRecord.timestamp: float`, already stored as Unix seconds.
- Produces: `JSONRPCEventMessage.timestamp: float`, serialized as a top-level optional JSON-RPC field.

- [ ] **Step 1: Write failing timestamp serialization tests**

Add tests that construct a live event without a timestamp and an explicit
replayed event:

```python
import time

from kimi_cli.wire.jsonrpc import JSONRPCEventMessage
from kimi_cli.wire.types import StepBegin


def test_jsonrpc_event_assigns_live_timestamp() -> None:
    before = time.time()
    message = JSONRPCEventMessage(params=StepBegin(n=1))
    after = time.time()

    assert before <= message.timestamp <= after
    assert message.model_dump(mode="json")["timestamp"] == message.timestamp


def test_jsonrpc_event_preserves_replay_timestamp() -> None:
    message = JSONRPCEventMessage(
        params=StepBegin(n=1),
        timestamp=1_721_234_567.25,
    )

    assert message.timestamp == 1_721_234_567.25
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run pytest tests/core/test_wire_message.py -v
```

from `kimi-cli/`.

Expected: the new tests fail because `JSONRPCEventMessage` has no `timestamp`.

- [ ] **Step 3: Add the live event timestamp**

In `wire/jsonrpc.py`, import `time` and Pydantic `Field`, then define:

```python
class JSONRPCEventMessage(_MessageBase):
    method: Literal["event"] = "event"
    params: Event
    timestamp: float = Field(default_factory=time.time)
```

Keep the existing serializers and validators unchanged.

- [ ] **Step 4: Preserve persisted timestamps during replay**

In `WireServer._handle_replay`, change the event branch to:

```python
elif is_event(wire_msg):
    await self._send_msg(
        JSONRPCEventMessage(params=wire_msg, timestamp=record.timestamp)
    )
    events += 1
```

All other live call sites use the default factory.

- [ ] **Step 5: Run the wire tests and verify GREEN**

Run:

```bash
uv run pytest tests/core/test_wire_message.py tests/core/test_wire_server_steer.py -v
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit the backend protocol change**

```bash
git add src/kimi_cli/wire/jsonrpc.py src/kimi_cli/wire/server.py tests/core/test_wire_message.py
git commit -m "feat(wire): expose persisted event timestamps"
```

Run from `kimi-cli/`.

### Task 2: Format and store assistant completion times

**Files:**
- Modify: `kimi-cli/web/src/hooks/wireTypes.ts`
- Modify: `kimi-cli/web/src/hooks/types.ts`
- Create: `kimi-cli/web/src/features/chat/message-output-time.ts`
- Create: `kimi-cli/web/src/features/chat/message-output-time.test.ts`
- Create: `kimi-cli/web/src/hooks/message-output-time.ts`
- Create: `kimi-cli/web/src/hooks/message-output-time.test.ts`
- Modify: `kimi-cli/web/src/hooks/useSessionStream.ts`

**Interfaces:**
- Consumes: optional `JsonRpcRequest.timestamp` in Unix seconds.
- Produces: optional `LiveMessage.completedAt` in Unix milliseconds and `MessageOutputTime({ completedAt })`.

- [ ] **Step 1: Write failing relative-time tests**

Create `message-output-time.test.ts` with fixed `now` values:

```ts
import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  formatMessageOutputTime,
  MessageOutputTime,
} from "./message-output-time.ts";

const minute = 60_000;
const hour = 60 * minute;
const day = 24 * hour;
const now = Date.UTC(2026, 6, 23, 9, 30, 0);

test("formats assistant output time boundaries", () => {
  assert.equal(formatMessageOutputTime(now - 30_000, now), "刚刚");
  assert.equal(formatMessageOutputTime(now - 5 * minute, now), "5分钟前");
  assert.equal(formatMessageOutputTime(now - 2 * hour, now), "2小时前");
  assert.equal(formatMessageOutputTime(now - 3 * day, now), "3天前");
  assert.equal(
    formatMessageOutputTime(now - 8 * day, now),
    new Date(now - 8 * day).toLocaleDateString(),
  );
});

test("renders semantic time markup", () => {
  const completedAt = now - 5 * minute;
  const markup = renderToStaticMarkup(
    createElement(MessageOutputTime, { completedAt, now }),
  );

  assert.match(markup, /<time/);
  assert.match(markup, /dateTime=/);
  assert.match(markup, /5分钟前/);
});
```

- [ ] **Step 2: Write failing message update tests**

Create `hooks/message-output-time.test.ts`:

```ts
import assert from "node:assert/strict";
import test from "node:test";
import { setMessageOutputTime } from "./message-output-time.ts";

test("sets completion time only on the selected assistant text message", () => {
  const messages = [
    { id: "a1", role: "assistant" as const, variant: "text" as const, content: "hi" },
    { id: "tool", role: "assistant" as const, variant: "tool" as const },
  ];

  const updated = setMessageOutputTime(messages, "a1", 1_721_234_567_000);

  assert.equal(updated[0]?.completedAt, 1_721_234_567_000);
  assert.equal(updated[1]?.completedAt, undefined);
  assert.notEqual(updated, messages);
});

test("leaves messages unchanged when timestamp or id is missing", () => {
  const messages = [
    { id: "a1", role: "assistant" as const, variant: "text" as const },
  ];

  assert.equal(setMessageOutputTime(messages, null, 1000), messages);
  assert.equal(setMessageOutputTime(messages, "a1", undefined), messages);
});
```

- [ ] **Step 3: Run the new Web tests and verify RED**

Run:

```bash
npx tsx --test \
  src/features/chat/message-output-time.test.ts \
  src/hooks/message-output-time.test.ts
```

from `kimi-cli/web/`.

Expected: FAIL because the modules and exports do not exist.

- [ ] **Step 4: Implement the formatter and time component**

Create `message-output-time.ts` without JSX so the Node test runner can import
it directly. Export:

```ts
export function formatMessageOutputTime(
  completedAt: number,
  now = Date.now(),
): string

export function MessageOutputTime({
  completedAt,
  now,
}: {
  completedAt: number;
  now?: number;
}): React.ReactElement
```

Use `createElement("time", ...)`, a one-minute `setInterval` in `useEffect`
when `now` is not supplied, `dateTime={date.toISOString()}`,
`title={date.toLocaleString()}`, and subdued action-row typography.

- [ ] **Step 5: Implement the pure message timestamp helper**

Create `hooks/message-output-time.ts`:

```ts
import type { LiveMessage } from "./types";

export function setMessageOutputTime(
  messages: LiveMessage[],
  messageId: string | null,
  completedAt: number | undefined,
): LiveMessage[] {
  if (!(messageId && completedAt !== undefined)) {
    return messages;
  }
  return messages.map((message) =>
    message.id === messageId &&
    message.role === "assistant" &&
    (!message.variant || message.variant === "text")
      ? { ...message, completedAt }
      : message,
  );
}
```

- [ ] **Step 6: Extend the protocol and message types**

Add to `JsonRpcRequest`:

```ts
timestamp?: number;
```

Add to `LiveMessage`:

```ts
/** Completion time for an assistant text message, Unix milliseconds. */
completedAt?: number;
```

- [ ] **Step 7: Propagate timestamps in the session stream**

Extend `processEvent` with `eventTimestamp?: number`. In the `ContentPart`
text branch, compute the current text message ID and update or create the
message with:

```ts
completedAt: eventTimestamp,
```

When parsing a JSON-RPC event, convert seconds to milliseconds:

```ts
const eventTimestamp =
  typeof message.timestamp === "number"
    ? message.timestamp * 1000
    : undefined;
processEvent(event, isReplayingRef.current, undefined, eventTimestamp);
```

Use `setMessageOutputTime` for updates so the pure helper is exercised by the
production path. Approval/question request calls continue to omit the fourth
argument.

- [ ] **Step 8: Run tests, type checking, and lint**

Run:

```bash
npx tsx --test \
  src/features/chat/message-output-time.test.ts \
  src/hooks/message-output-time.test.ts
npm run typecheck
npm run lint
```

Expected: all commands pass.

- [ ] **Step 9: Commit the Web data and formatting layer**

```bash
git add \
  web/src/hooks/wireTypes.ts \
  web/src/hooks/types.ts \
  web/src/hooks/useSessionStream.ts \
  web/src/hooks/message-output-time.ts \
  web/src/hooks/message-output-time.test.ts \
  web/src/features/chat/message-output-time.ts \
  web/src/features/chat/message-output-time.test.ts
git commit -m "feat(web): retain assistant output times"
```

Run from `kimi-cli/`.

### Task 3: Render the time after Copy and Fork

**Files:**
- Modify: `kimi-cli/web/src/features/chat/components/virtualized-message-list.tsx`
- Test: `kimi-cli/web/src/features/chat/message-output-time.test.ts`

**Interfaces:**
- Consumes: `LiveMessage.completedAt` and `MessageOutputTime`.
- Produces: Copy → Fork → relative time ordering inside `MessageActions`.

- [ ] **Step 1: Add a failing action-row eligibility test**

Export a pure predicate from `message-output-time.ts`:

```ts
export function shouldShowMessageOutputTime(message: LiveMessage): boolean
```

Add tests proving it returns true only for a non-streaming assistant text
message with `completedAt`, and false for streaming, user, tool, thinking, and
missing-time messages.

- [ ] **Step 2: Run the eligibility test and verify RED**

Run:

```bash
npx tsx --test src/features/chat/message-output-time.test.ts
```

Expected: FAIL because `shouldShowMessageOutputTime` is not exported.

- [ ] **Step 3: Implement eligibility and render ordering**

Implement the predicate, import `MessageOutputTime` and the predicate into
`virtualized-message-list.tsx`, and append this after the existing Fork button:

```tsx
{shouldShowMessageOutputTime(message) && (
  <MessageOutputTime completedAt={message.completedAt!} />
)}
```

Do not change the existing `hover-reveal` behavior.

- [ ] **Step 4: Run complete Web verification**

Run:

```bash
npx tsx --test \
  src/features/chat/message-output-time.test.ts \
  src/hooks/message-output-time.test.ts \
  src/lib/approval-snapshot.test.ts
npm run typecheck
npm run lint
npm run build
```

Expected: all tests, type checking, lint, and the production build pass.

- [ ] **Step 5: Commit the UI integration**

```bash
git add \
  web/src/features/chat/components/virtualized-message-list.tsx \
  web/src/features/chat/message-output-time.ts \
  web/src/features/chat/message-output-time.test.ts
git commit -m "feat(web): show assistant output time in message actions"
```

Run from `kimi-cli/`.

### Task 4: Verify integration and parent repository pointer

**Files:**
- Modify: `kimi-cli` submodule pointer in the parent repository.
- Verify: `packaging/venvstacks.resolved.toml` remains uncommitted.

**Interfaces:**
- Consumes: the completed `kimi-cli` commits.
- Produces: a parent repository commit referencing the timestamp-enabled submodule.

- [ ] **Step 1: Run focused backend and complete Web verification**

Run:

```bash
uv run pytest \
  tests/core/test_wire_message.py \
  tests/core/test_wire_server_steer.py -q
npm --prefix web run typecheck
npm --prefix web run lint
npm --prefix web run build
npm --prefix web exec -- tsx --test \
  src/features/chat/message-output-time.test.ts \
  src/hooks/message-output-time.test.ts \
  src/lib/approval-snapshot.test.ts
```

from `kimi-cli/`.

Expected: all commands exit `0`.

- [ ] **Step 2: Inspect repository state**

Run:

```bash
git status --short
git -C kimi-cli status --short
git diff --check
git -C kimi-cli diff --check
```

from the parent repository.

Expected: the submodule is clean; the parent shows the changed `kimi-cli`
pointer and the pre-existing local-only
`packaging/venvstacks.resolved.toml` modification.

- [ ] **Step 3: Commit only the submodule pointer**

```bash
git add kimi-cli
git commit -m "feat: show assistant message output times"
```

Do not stage `packaging/venvstacks.resolved.toml`.
