"""Guardrails: deterministic checks around the LLM.

Three layers, each cheap and testable without a model:

1. **Input redaction**: remove personal data *before* it reaches the LLM or the tracing tool.
   GDPR's data-minimisation principle: don't send a third party what it doesn't need. The model
   doesn't need the card number to know the ticket is about a double charge.
2. **Injection heuristics**: catch the obvious "ignore your instructions" attacks with patterns.
   It's free and instant, but easy to get around, which is why an LLM check runs as well (see
   `llm.check_injection`). That's defence in depth.
3. **Output checks**: the reply must not leak personal data or promise things only a human may
   promise (refunds, compensation). These are the failures that become complaints to the
   Financial Ombudsman.
"""

import re

# --- 1. PII redaction --------------------------------------------------------------------------

# Order matters: IBANs contain digit runs that would otherwise look like account numbers.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){12,18}\d\b")),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("PHONE", re.compile(r"(?:\+44\s?7\d{3}|\b07\d{3})\s?\d{3}\s?\d{3}\b")),
    ("SORT_CODE", re.compile(r"\b\d{2}-\d{2}-\d{2}\b")),
    ("ACCOUNT", re.compile(r"\b\d{8}\b")),
]


def _luhn_ok(digits: str) -> bool:
    """Card numbers end in a check digit (the Luhn algorithm). This tells a real card number
    apart from, say, a 16-digit order reference, so we don't over-redact."""
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def redact(text: str) -> tuple[str, list[str]]:
    """Return (text with PII replaced by [TYPE] placeholders, list of types found)."""
    found: list[str] = []

    for label, pattern in _PATTERNS:

        def replace(m: re.Match, label=label) -> str:
            if label == "CARD" and not _luhn_ok(re.sub(r"\D", "", m.group())):
                return m.group()  # not a real card number, leave it alone
            found.append(label)
            return f"[{label}]"

        text = pattern.sub(replace, text)
    return text, found


# --- 2. Prompt-injection heuristics ------------------------------------------------------------

_INJECTION_PATTERNS = [
    r"ignore (all |any |the |your |previous |prior |above )+(instructions|rules|prompts?)",
    r"disregard (all |any |the |your |previous |prior )*(instructions|rules|guidelines)",
    # "your/system" required: "show me the instructions for disputing" is a normal request
    r"(reveal|show|print|repeat) (me )?(the text of )?(your (system )?|the system )(prompt|instructions)",
    r"\byou are now\b",
    r"\b(developer|admin|god|jailbreak|DAN) mode\b",
    r"\bact as (an? )?(admin|supervisor|manager|system)\b",
    r"\bnew instructions?\b",
    r"</?(system|ticket|articles)>",  # trying to break out of our XML delimiters
    r"\b(approve|authori[sz]e|confirm|process) (my|the|this) (refund|compensation|payment)\b",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


def injection_heuristics(text: str) -> list[str]:
    """Return the suspicious phrases found (empty list = nothing obvious)."""
    return [m.group() for m in _INJECTION_RE.finditer(text)]


# --- Scam signals ------------------------------------------------------------------------------

_SCAM_SIGNALS = re.compile(
    r"\b(is (it|this|that) (genuine|real|legit)|was (that|it) (really )?you|safe account"
    r"|(text|sms|email|call|message|whatsapp)\b.{0,60}\b(link|pin|verify|unlock|card details)"
    r"|(link|pin|verify|unlock)\b.{0,60}\b(text|sms|email|call|message|whatsapp))",
    re.IGNORECASE,
)


def scam_signals(text: str) -> list[str]:
    """Phrases suggesting the customer may be the target of a scam. Safety-critical, so this is a
    deterministic rule that backs up the classifier prompt instead of trusting it alone."""
    return [m.group() for m in _SCAM_SIGNALS.finditer(text)]


# --- 3. Output checks --------------------------------------------------------------------------

_FORBIDDEN_PROMISES = re.compile(
    r"\b(refund|compensation|payment) (has been|is being|was|is) "
    r"(approved|processed|issued|authori[sz]ed|sent)\b"
    r"|\bI(?:'ve| have) (refunded|approved|reversed|credited)\b"
    r"|\bwe(?:'ll| will) (refund|compensate|credit) you\b"
    r"|\bguarantee\b",
    re.IGNORECASE,
)


# Banks never tell customers to use a link they were sent, or to share secrets.
_UNSAFE_ADVICE = re.compile(
    r"\b(click|follow|tap|use|open) (on )?(the|this|that) link\b"
    r"|\b(click|tap)(ing)? (here|below)\b"
    r"|\b(send|share|give|tell) (us |me )?(your )?(pin|password|full card number)\b",
    re.IGNORECASE,
)


# CR-001: the model invented "https://example.com/kb-002" in a live reply. A link in a bank's
# message is a phishing pattern customers are trained to distrust, and a made-up one is worse.
_LINKS_OR_IDS = re.compile(
    r"https?://\S+|(?<![\w:])//[\w.-]+\.\w+\S*|\bwww\.\S+|\[[^\]]*\]\([^)]*\)|\[?\bkb-\d{3}\b\]?",
    re.IGNORECASE,
)


# The negation must sit directly before the matched instruction: "Never share your PIN" and
# "Please do not click the link" are safe. In "Do not wait; share your PIN" or "Do not hesitate
# to share your PIN", the negation belongs to another verb, so the instruction is still unsafe.
# (Two rounds of code review on PR #3 found those bypasses.)
_NEGATION = re.compile(r"\b(not|never|don't|don’t|avoid)\s+$", re.IGNORECASE)


def output_violations(reply: str) -> list[str]:
    """Problems with a drafted reply that mean a human must look at it before it's sent."""
    problems = []
    _, pii = redact(reply)
    if pii:
        problems.append(f"reply contains personal data ({', '.join(sorted(set(pii)))})")
    if m := _FORBIDDEN_PROMISES.search(reply):
        problems.append(f"reply makes a promise only a human may make: '{m.group()}'")
    for m in _UNSAFE_ADVICE.finditer(reply):
        # "Please do not click the link" is the *right* advice. The first version flagged it,
        # and the CR-001 eval run caught that false positive.
        before = reply[max(0, m.start() - 25) : m.start()].lower()
        if not _NEGATION.search(before):
            problems.append(f"reply gives unsafe advice: '{m.group()}'")
            break
    if m := _LINKS_OR_IDS.search(reply):
        problems.append(f"reply contains a link or internal article ID: '{m.group()}'")
    return problems
