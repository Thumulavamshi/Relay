"""Moments a human should take over: an explicit ask for a person, a dispute,
distress, or a request to stop calling.

Deterministic, like the classifier's rules overlay - it runs on every lead turn,
costs nothing, and its job is to page the sales team, not to judge the lead.
Only the LEAD's words are read; the agent saying "let me get a colleague" is not
a request.

Indic patterns carry no \\b: Python's \\b is defined by \\w, which excludes the
combining marks inside Hindi and Telugu words, so a boundary there never matches
(the same trap timeparse.py documents).
"""

import re

_RULES = (
    ("asked for a human",
     r"\b(speak|talk)\s+(to|with)\s+(a\s+|an\s+|some\s*one\s+|your\s+)?(real\s+)?"
     r"(person|human|manager|supervisor|owner|somebody|someone)\b"
     r"|\breal\s+person\b"
     r"|insaan\s+se\s+baat|manager\s+se\s+baat|kisi\s+(aadmi|insaan)\s+se"
     r"|इंसान\s*से\s*बात|मैनेजर\s*से\s*बात"
     r"|manishi\s*tho\s+matlad|manager\s*tho\s+matlad"
     r"|మనిషితో\s*మాట్లాడ|మేనేజర్\s*తో\s*మాట్లాడ"),
    ("dispute or complaint",
     r"\b(complaint|complain|fraud|scam|cheat(ed|ing)?|refund|legal\s+action|lawyer"
     r"|consumer\s+court|police)\b"
     r"|शिकायत|धोखा|ठग|ఫిర్యాదు|మోసం"),
    ("vulnerable or distressed",
     r"\b(in\s+(the\s+)?hospital|passed\s+away|someone\s+died|funeral|i'?m\s+not\s+well"
     r"|i\s+am\s+not\s+well|medical\s+emergency)\b"
     r"|अस्पताल|ఆసుపత్రి|హాస్పిటల్"),
    ("asked not to be called again",
     r"\b(don'?t|do\s+not|stop)\s+call(ing)?\s+me\b|\bremove\s+my\s+number\b"
     r"|\bnever\s+call\s+(me\s+)?again\b"
     r"|फोन\s*मत\s*कर|call\s+cheyakandi|ఫోన్\s*చేయకండి"),
)

_COMPILED = [(reason, re.compile(pattern, re.I)) for reason, pattern in _RULES]


def detect(text):
    """The reason a human should step in, or None."""
    for reason, rx in _COMPILED:
        if rx.search(text or ""):
            return reason
    return None
