"""Targeted Mono walks that mirror mtga-tracker-daemon's HTTP endpoints.

Each function returns a dict shaped like the daemon's JSON response so
:mod:`client.overlay.arena_memory` can convert directly to its dataclasses.
Returns ``None`` when the underlying chain is not yet populated (Arena still
loading, the user is signed out, etc.) — callers should treat ``None`` as
"unavailable, retry next poll".
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .exceptions import MonoFieldMissing
from .mono import AssemblyImage, ClassDefinition, ObjectInstance
from .session import MemorySession

logger = logging.getLogger(__name__)


_EVENT_PAGE_TYPE = "EventPage.EventPageContentController"


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

    **STUB — pending live-pod investigation.**

    The mtga-tracker-daemon does not expose this data, so the field paths
    are not yet known. They must be discovered by walking ``WrapperController``
    / ``PAPA`` / ``EventPageContentController.PlayerEvent.CourseData``
    against a live draft pod (pack 1 pick 1) using
    ``scripts/diag_draft_state.py``. See ``docs/draft-state-investigation.md``
    for the procedure.

    Once the field paths are filled in, the return shape should be:

    .. code-block:: python

        {
            "is_active": bool,           # in a live draft right now
            "event_name": str,           # e.g. "PremierDraft_SOS_20260421"
            "pack_number": int,          # 0-indexed
            "pick_number": int,          # 0-indexed
            "current_pack": list[int],   # Arena grpIds presented this pick
            "picked_cards": list[int],   # Arena grpIds picked so far this draft
        }

    Returns ``None`` when not in a draft (or while the field paths remain
    unknown). :class:`client.overlay.memory_watcher.MemoryWatcher` treats
    ``None`` as "no live draft" and emits no events.
    """
    _ = session  # unused until investigation completes
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
