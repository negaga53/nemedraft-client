# Phase 2 — Live draft state investigation

The Phase 2 scaffolding (MemoryWatcher polling thread + dedupe in
`OverlayApp._on_event`) is in place, but the field paths for live draft
state (current pack contents, picked cards, pack/pick numbers) still need
to be discovered against a live MTG Arena pod.
`client.overlay.memory.walker.read_draft_state` is currently a stub that
returns `None`, which is why MemoryWatcher emits no events today. Once the
field paths are filled in, Phase 2 lights up automatically.

## Why this is a runtime investigation, not a code-reading one

`mtga-tracker-daemon` does **not** expose draft pack/pick state — it only
reads identity / inventory / current-event / match-rank. There is no prior
art for "where in MTGA's Mono heap is the live draft pack stored" that
maps directly to today's MTGA build. We have to find it by walking the
object graph during a live pod and looking for the right shapes (`int[]`
arrays of grpIds, integer pack/pick counters, etc.).

## When to run

Sit pod 1 pick 1 of any draft (Premier Draft is fine; the structure is
the same across draft formats). The pack must be visible — i.e. you can
see the 14-15 cards but have not yet clicked one.

## Procedure

### 1. Capture pack 1 pick 1 state

```cmd
cd C:\Users\home\Documents\Projects\NemeDraft\external\nemedraft-client
py -3 scripts\diag_draft_state.py > diag_p1p1.txt
```

The script dumps every field on these objects:

* `WrapperController.<Instance>k__BackingField` (top-level singleton)
* `SceneLoader.<CurrentNavContent>k__BackingField` →
  `EventPageContentController._currentEventContext.PlayerEvent` →
  `<CourseData>k__BackingField`
* `PAPA` static singleton (multiple field-name candidates tried)
* All registered classes whose name contains `Draft`, `Pack`, or `Pick`

For object fields the script descends one to two levels deep and prints
the runtime class name. Any `Int32[]` instance encountered gets its
length and first/last few values dumped — those are the smoking-gun
signatures of card grpId arrays.

### 2. Make the pick — capture pack 1 pick 2

After clicking a card and letting MTGA advance to pick 2, run again:

```cmd
py -3 scripts\diag_draft_state.py > diag_p1p2.txt
```

### 3. Diff the two dumps

```cmd
fc /n diag_p1p1.txt diag_p1p2.txt > diag_diff.txt
```

(`fc` is Windows' built-in equivalent of `diff`.)

What to look for:

* An `int` field that went from `0` → `1`: that's `pick_number`.
* An `int[]` whose length dropped by 1 (a card was removed from the
  pack-cards list when you picked it): that's `current_pack`.
* An `int[]` whose length grew by 1: that's `picked_cards`.
* A `bool` or `int` flag that flipped between the two states: candidate
  for `is_active`.
* Any field whose value matches the `internal_event_name` string we
  already pull (`PremierDraft_SOS_…`): that's `event_name`.

Record the field paths — the chain of class+field names from
`WrapperController.<Instance>k__BackingField` down to each value.

### 4. Confirm with pack 2 pick 1

After pick 14 of pack 1, MTGA auto-rotates to pack 2. Re-run the script
once seated for pack 2 pick 1:

```cmd
py -3 scripts\diag_draft_state.py > diag_p2p1.txt
```

Verify:

* `pack_number` went from `0` → `1` (or `1` → `2` if 1-indexed).
* `pick_number` reset to `0`.
* `current_pack` is fresh (14-15 new grpIds).
* `picked_cards` retained the picks from pack 1 (14 of them).

### 5. Confirm draft end

Either complete the draft to deckbuild, or back out — re-run once and
note what changes between "draft active" and "draft over". A clean
boolean like `_isDraftActive` flipping to `False`, or `CourseData`
becoming `null`, is what we'd hope for.

```cmd
py -3 scripts\diag_draft_state.py > diag_postdraft.txt
fc /n diag_p1p1.txt diag_postdraft.txt > diag_endgame.txt
```

## What to fill in afterwards

Once the field paths are known, edit
`client/overlay/memory/walker.py:read_draft_state` and replace the
single-line stub with a chain like:

```python
wrapper_controller = image.get_class("WrapperController")
wrapper = _coerce_object(wrapper_controller.get_static("<Instance>k__BackingField"))
draft = _follow(wrapper, [
    "<SceneLoader>k__BackingField",
    "<CurrentNavContent>k__BackingField",
    "_currentEventContext",
    "PlayerEvent",
    "<CourseData>k__BackingField",
    # ... whatever turns out to hold the pack array, picks, indices ...
])
if draft is None:
    return None

reader = image.reader
pack_addr = ...  # pointer derived from a draft field
return {
    "is_active":    True,
    "event_name":   _read_string_field(eventinfo, "InternalEventName") or "",
    "pack_number":  _read_int_field(draft, "<PackNumber>k__BackingField"),
    "pick_number":  _read_int_field(draft, "<PickNumber>k__BackingField"),
    "current_pack": reader.read_int32_array(pack_addr) or [],
    "picked_cards": reader.read_int32_array(picks_addr) or [],
}
```

Use `ProcessReader.read_int32_array(address)` for the grpId arrays — it
already handles the Mono `int[]` layout (length at `+0x18`, vector data
at `+0x20`). Wire `_follow` and `_coerce_object` from `walker.py`.

Then re-run the smoke test (`scripts\test_arena_memory.py`) and the
MemoryWatcher emit logs (look for `MemoryWatcher emit: PackEvent` lines)
to confirm.

## Why MemoryWatcher is safe to run today

While `read_draft_state` returns `None`, MemoryWatcher attaches, polls
every 250 ms, and emits nothing. There is no risk of duplicate events
spamming `_on_event`. The dedupe guard already in place will swallow any
duplicates if/when memory and log sources both fire after the field
paths are filled in.
