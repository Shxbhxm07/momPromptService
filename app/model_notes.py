"""Drop the model's own notes when they reach the minutes as if they were meeting business.

WHY. A real cluster job (t4, 2026-09-18) printed, in the official minutes:

    799. Decision. Here are the action items extracted from the meeting transcript:.
    805. Decision. Note: The owners and due dates for these action items are not always explicitly
         stated in the transcript, so some assumptions have been made...
    767.1. Here are the figures mentioned in the transcript.

and, among the points, "Note: Some of the points may be mentioned multiple times in the conversation,
but I have only listed each point once in the above summary." None of it happened at the meeting: it
is the model introducing and footnoting its own lists.

The writer now skips such lines where its bullet replies are split into items (_bullets_to_list), which
is where these came from. This sweep runs over the finished minutes as well, so a note arriving by any
other path — a JSON pass, a synthesis — is caught too. The test is one function,
llama.utils.text_utils.is_model_note, shared by both places so they cannot drift apart.
"""
from typing import Any, Dict, List

from llama.utils.text_utils import is_model_note

# String lists in the minutes. Attendees are not text lines, and the summary is prose.
_LISTS = ("key_points", "decisions", "key_figures", "agenda")


def strip(mom: Dict[str, Any]) -> List[str]:
    """Remove every line that is the model's own note, in place. Returns the lines removed."""
    dropped: List[str] = []
    for key in _LISTS:
        kept = []
        for line in mom.get(key) or []:
            (dropped if is_model_note(str(line)) else kept).append(line)
        mom[key] = kept
    actions = []
    for ai in mom.get("action_items") or []:
        task = str((ai.get("task") if isinstance(ai, dict) else ai) or "")
        if is_model_note(task):
            dropped.append(task)
        else:
            actions.append(ai)
    mom["action_items"] = actions
    return [str(x) for x in dropped]
