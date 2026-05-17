# Phase 2 — Live draft state (implemented)

`client.overlay.memory.walker.read_draft_state` now returns live pack /
pick / picked-card state during a draft. MemoryWatcher polls it every
250 ms and emits the same `PackEvent` / `PickEvent` / `DraftStartEvent`
/ `DraftEndEvent` / `DraftCompleteEvent` dataclasses as LogWatcher.
Identical events fired by both sources within a 2 s window are
deduplicated in `OverlayApp._on_event` — first source wins.

## Field chain (verified MTGA 2026.58.20.12269, QuickDraft pod)

```
WrapperController.<Instance>k__BackingField
  .<SceneLoader>k__BackingField
  .<CurrentNavContent>k__BackingField                # must be DraftContentController
  ._limitedEvent                                      # Wotc.Mtga.Events.LimitedPlayerEvent
  .<DraftPod>k__BackingField                          # BotDraftPod | HumanDraftPod
    .<InternalEventName>k__BackingField  : STRING    # event_name
    ._currentPack                        : I4        # pack_number  (0-indexed)
    ._currentPick                        : I4        # pick_number  (0-indexed)
    ._currentPackCards                   : List<int> # current_pack (grpIds)
    .<PickedCards>k__BackingField        : List<int> # picked_cards (null pre-pick-1)
```

Returning `None` (CurrentNavContent type ≠ DraftContentController, or the
chain has not populated yet) is the signal MemoryWatcher uses to emit a
`DraftEndEvent` if it previously saw an active draft.

`BotDraftPod` and `HumanDraftPod` both implement `IDraftPod`; the field
lookup goes through `ObjectInstance.get(name)`, which walks inheritance,
so the same code path works for either.

## Re-verifying after an MTGA build update

If a future MTGA build renames or reorders fields, the symptom will be
`read_draft_state` returning `None` while in a pod (or returning
nonsense values). To re-discover the layout:

```cmd
cd C:\Users\home\Documents\Projects\NemeDraft\external\nemedraft-client
.venv\Scripts\python.exe scripts\diag_draft_state.py    > diag_p1p1.txt
.venv\Scripts\python.exe scripts\diag_draft_targeted.py  diag_targeted.txt
```

`diag_draft_targeted.py` writes UTF-8 directly to disk (Windows' default
cp1252 stdout chokes on non-ASCII MTGA strings) and dumps the full
chain under `DraftContentController._limitedEvent.<DraftPod>...` — the
fields named above should be present.

Smoke-test the implementation after an update:

```cmd
.venv\Scripts\python.exe scripts\diag_smoke_draft_state.py
```

That prints the live `read_draft_state(...)` payload as JSON — pack
should be 13–15 grpIds, picked should grow by 1 after each pick.

## What deduplication looks like in practice

Both watchers run concurrently. The dedupe signature is
`(pack_number, pick_number, tuple(card_grpids))` for `PackEvent` and
`PickEvent`. Whichever source delivers an event first wins; the second
is dropped silently. The 2 s window is enough to cover the 250 ms
memory poll plus log-tail latency without swallowing legitimate
repeated picks across separate drafts.
