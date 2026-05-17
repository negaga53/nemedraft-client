"""Targeted Mono walks that mirror mtga-tracker-daemon's HTTP endpoints.

Each function returns a dict shaped like the daemon's JSON response so
:mod:`client.overlay.arena_memory` can convert directly to its dataclasses.
Returns ``None`` when the underlying chain is not yet populated (Arena still
loading, the user is signed out, etc.) — callers should treat ``None`` as
"unavailable, retry next poll".
"""

from __future__ import annotations

import logging
import struct
import time
from typing import Any

from .exceptions import MonoFieldMissing
from .mono import AssemblyImage, ClassDefinition, ObjectInstance
from .reader import ProcessReader
from .session import MemorySession

logger = logging.getLogger(__name__)


_EVENT_PAGE_TYPE = "EventPage.EventPageContentController"
_DRAFT_CONTENT_TYPE = "Wotc.Mtga.Wrapper.Draft.DraftContentController"


def read_player_identity(session: MemorySession) -> dict[str, Any] | None:
    """Read MTG Arena account identity from process memory.

    Mirrors HackF5.UnitySpy chain in ``HttpServer.cs:151-185``. The Arena
    persona ID (not the Wizards account ID) is what every other system in
    NemeDraft keys off of, so it's surfaced as ``playerId``.

    .. code-block:: text

        WrapperController.<Instance>k__BackingField
            .<AccountClient>k__BackingField
            .<AccountInformation>k__BackingField
            -> { AccountID, DisplayName, PersonaID }
    """
    started = time.monotonic()
    image = session.image
    if image is None:
        return None
    try:
        wrapper_controller = image.get_class("WrapperController")
        if wrapper_controller is None:
            logger.debug("Identity walk: WrapperController class not in image cache")
            return None
        wrapper = _coerce_object(wrapper_controller.get_static("<Instance>k__BackingField"))
        if wrapper is None:
            logger.debug("Identity walk: WrapperController.Instance is null")
            return None
        account_client = _coerce_object(wrapper.get("<AccountClient>k__BackingField"))
        if account_client is None:
            logger.debug("Identity walk: WrapperController.AccountClient is null (sign-in pending?)")
            return None
        account = _coerce_object(account_client.get("<AccountInformation>k__BackingField"))
        if account is None:
            logger.debug("Identity walk: AccountClient.AccountInformation is null (sign-in pending?)")
            return None
        player_id = _read_string_field(account, "PersonaID")
        if not player_id:
            logger.debug("Identity walk: AccountInformation.PersonaID is empty")
            return None
        return {
            "playerId": player_id,
            "displayName": _read_string_field(account, "DisplayName"),
            "elapsedTime": int((time.monotonic() - started) * 1000),
        }
    except MonoFieldMissing as exc:
        logger.debug("Identity walk: %s", exc)
        return None
    except Exception:
        logger.warning("Unexpected error reading Arena identity", exc_info=True)
        return None


def read_current_event(session: MemorySession) -> dict[str, Any] | None:
    """Read MTG Arena current event/lobby state from process memory.

    Mirrors HackF5.UnitySpy chain in ``HttpServer.cs:208-243``:

    .. code-block:: text

        WrapperController.<Instance>k__BackingField
            .<SceneLoader>k__BackingField
            .<CurrentNavContent>k__BackingField

        if runtime type == "EventPage.EventPageContentController":
            ._currentEventContext.PlayerEvent
            ._eventInfo  -> { InternalEventName, EventState, FormatType }
            .<CourseData>k__BackingField -> { DraftId, CurrentEventState, CurrentModule }
    """
    started = time.monotonic()
    image = session.image
    if image is None:
        return None
    try:
        wrapper_controller = image.get_class("WrapperController")
        if wrapper_controller is None:
            return None
        wrapper = _coerce_object(wrapper_controller.get_static("<Instance>k__BackingField"))
        current = _follow(wrapper, ["<SceneLoader>k__BackingField",
                                    "<CurrentNavContent>k__BackingField"])
        if current is None:
            return {
                "isInEventLobby": False,
                "contentType": "",
                "elapsedTime": int((time.monotonic() - started) * 1000),
            }
        klass = current.runtime_class()
        content_type = klass.full_name if klass is not None else ""
        is_in_lobby = content_type == _EVENT_PAGE_TYPE
        result: dict[str, Any] = {
            "isInEventLobby": is_in_lobby,
            "contentType": content_type,
        }
        if is_in_lobby:
            event_info = _follow(current, ["_currentEventContext",
                                           "PlayerEvent",
                                           "_eventInfo"])
            course_data = _follow(current, ["_currentEventContext",
                                            "PlayerEvent",
                                            "<CourseData>k__BackingField"])
            if event_info is not None:
                result["internalEventName"] = _read_string_field(event_info, "InternalEventName") or ""
                result["eventState"] = _read_int_field(event_info, "EventState")
                result["formatType"] = _read_int_field(event_info, "FormatType")
            if course_data is not None:
                result["draftId"] = _read_string_field(course_data, "DraftId") or ""
                result["currentEventState"] = _read_int_field(course_data, "CurrentEventState")
                result["currentModule"] = _read_int_field(course_data, "CurrentModule")
        result["elapsedTime"] = int((time.monotonic() - started) * 1000)
        return result
    except MonoFieldMissing as exc:
        logger.debug("Current event walk: %s", exc)
        return None
    except Exception:
        logger.warning("Unexpected error reading Arena current event", exc_info=True)
        return None


def read_draft_state(session: MemorySession) -> dict[str, Any] | None:
    """Read live draft pack/pick state from process memory.

    Chain (verified against MTGA build 2026.58.20.12269 on a live
    QuickDraft pod; same field names apply for HumanDraftPod via the
    shared ``IDraftPod`` interface):

    .. code-block:: text

        WrapperController.<Instance>k__BackingField
            .<SceneLoader>k__BackingField
            .<CurrentNavContent>k__BackingField        # DraftContentController iff in draft
            ._limitedEvent                              # Wotc.Mtga.Events.LimitedPlayerEvent
            .<DraftPod>k__BackingField                  # BotDraftPod | HumanDraftPod
              .<InternalEventName>k__BackingField : str
              ._currentPack                       : I4
              ._currentPick                       : I4
              ._currentPackCards                  : List<int>
              .<PickedCards>k__BackingField       : List<int>?

    Returns ``None`` when not in a draft (CurrentNavContent type mismatches,
    or the draft pod / event chain has not populated yet). MemoryWatcher
    treats ``None`` as "no live draft" and emits a ``DraftEndEvent`` if it
    previously saw an active draft.
    """
    image = session.image
    if image is None:
        return None
    try:
        wrapper_controller = image.get_class("WrapperController")
        if wrapper_controller is None:
            return None
        wrapper = _coerce_object(wrapper_controller.get_static("<Instance>k__BackingField"))
        current = _follow(wrapper, [
            "<SceneLoader>k__BackingField",
            "<CurrentNavContent>k__BackingField",
        ])
        if current is None:
            return None
        klass = current.runtime_class()
        if klass is None or klass.full_name != _DRAFT_CONTENT_TYPE:
            return None

        draft_pod = _follow(current, [
            "_limitedEvent",
            "<DraftPod>k__BackingField",
        ])
        if draft_pod is None:
            return None

        event_name = (
            _read_string_field(draft_pod, "<InternalEventName>k__BackingField")
            or ""
        )
        pack_number = _read_int_field(draft_pod, "_currentPack")
        pick_number = _read_int_field(draft_pod, "_currentPick")

        reader = image.reader
        current_pack = _read_list_int32(
            reader, _coerce_object(draft_pod.get("_currentPackCards")),
        )
        picked_cards = _read_list_int32(
            reader, _coerce_object(draft_pod.get("<PickedCards>k__BackingField")),
        )

        return {
            "is_active": True,
            "event_name": event_name,
            "pack_number": pack_number,
            "pick_number": pick_number,
            "current_pack": current_pack,
            "picked_cards": picked_cards,
        }
    except MonoFieldMissing as exc:
        logger.debug("Draft state walk: %s", exc)
        return None
    except Exception:
        logger.warning("Unexpected error reading Arena draft state", exc_info=True)
        return None


# -------- helpers ----------------------------------------------------------

def _coerce_object(value: Any) -> ObjectInstance | None:
    if isinstance(value, ObjectInstance) and value.address != 0:
        return value
    return None


def _follow(start: ObjectInstance | None, fields: list[str]) -> ObjectInstance | None:
    """Chain object-field reads, short-circuiting on null or wrong type."""
    current = start
    for name in fields:
        if current is None:
            return None
        next_value = current.get(name)
        current = _coerce_object(next_value)
    return current


def _read_string_field(obj: ObjectInstance, name: str) -> str:
    value = obj.get(name)
    return value if isinstance(value, str) else ""


def _read_int_field(obj: ObjectInstance, name: str) -> int:
    value = obj.get(name)
    return value if isinstance(value, int) else 0


def _read_list_int32(
    reader: ProcessReader | None,
    list_obj: ObjectInstance | None,
    *,
    max_elements: int = 256,
) -> list[int]:
    """Read a Mono ``List<int>`` (or ``List<uint>``) and return its elements.

    Mono ``System.Collections.Generic.List`1`` layout (x64):

    * MonoObject header: 2 pointers
    * ``_items``: pointer to backing ``int[]`` (offset ``2 * size_of_ptr``)
    * ``_size``: int32 logical count (offset ``3 * size_of_ptr``)

    The backing array follows the standard MonoArray layout — its own
    length sits at ``+3 * size_of_ptr`` and the data starts at
    ``+4 * size_of_ptr``. ``_size`` may be smaller than the array's
    capacity, so we clamp.

    Returns ``[]`` whenever the list pointer is NULL, the size is out of
    range, or any memory read fails.
    """
    if reader is None or list_obj is None or not list_obj.address:
        return []
    sp = reader.size_of_ptr
    try:
        items_ptr = reader.read_ptr(list_obj.address + sp * 2)
        size = reader.read_int32(list_obj.address + sp * 3)
    except Exception:
        return []
    if items_ptr == 0 or size <= 0:
        return []
    if size > max_elements:
        logger.debug("List<int> size=%d exceeds max %d, ignoring", size, max_elements)
        return []
    try:
        arr_length = reader.read_int32(items_ptr + sp * 3)
    except Exception:
        return []
    n = min(size, max(arr_length, 0))
    if n <= 0:
        return []
    try:
        data = reader.read_bytes(items_ptr + sp * 4, 4 * n)
    except Exception:
        return []
    try:
        return list(struct.unpack(f"<{n}i", data))
    except struct.error:
        return []
