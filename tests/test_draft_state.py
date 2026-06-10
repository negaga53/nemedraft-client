from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client.overlay.draft_state import extract_set_code


def test_mwm_sos_cascade_bot_draft_maps_to_sos() -> None:
    assert extract_set_code("MWM_SOS_Cascade_BotDraft_20260609") == "SOS"


def test_sos_cascade_bot_draft_context_maps_to_sos() -> None:
    assert extract_set_code("SOS_Cascade_BotDraft") == "SOS"


def test_cas_bot_draft_aliases_to_sos() -> None:
    assert extract_set_code("CAS_BotDraft_20260609") == "SOS"


def test_bot_draft_date_is_not_treated_as_set_code() -> None:
    assert extract_set_code("MWM_Cascade_BotDraft_20260609") is None
