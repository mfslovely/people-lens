"""Input and output guardrails. These are deterministic checks that run whether or not the LLM behaves.

Input guard: rule-based detection of prompt injection, role spoofing, destructive requests and secret extraction.
It blocks before any LLM call, so a jailbreak cannot reach the model or the tools at all. It is a first line of defence
only: authorization in the tools is what actually enforces access, so a bypassed guard still cannot escalate privileges.

Output guard: grounding checks on the final answer.
  * every number must appear in tool outputs, the user message or the system context
  * policy citation markers [P#] must refer to evidence that was actually retrieved
  * email addresses must come from tool output
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.agent.models import GuardVerdict

_RULES: list[tuple[str, re.Pattern, str]] = [
    ("injection", re.compile(
        r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(previous|prior|above|earlier|all|your|system|safety)\b.{0,30}"
        r"\b(instructions?|rules?|prompts?|guardrails?|restrictions?|polic(y|ies))\b"
        r"|\b(developer|god|admin|sudo|jailbreak|dan)\s+mode\b|\bjailbreak\b|\byou are now\b(?!.{0,20}\b(on|in)\b)", re.I),
     "This looks like an attempt to override the assistant's instructions, so it was not processed."),
    ("role_spoof", re.compile(
        r"\b(pretend|act|behave|imagine|assume|treat me)\b.{0,25}\b(i am|i'm|me as|to be|as|like)\b.{0,15}"
        r"\b(an? |the )?(hr|admin|administrator|manager|ceo|superuser|root)\b"
        r"|\b(make|grant|give|elevate|switch)\b.{0,15}\b(me|my role|my access)\b.{0,20}\b(admin|hr|manager|privileges?|access)\b"
        r"|\bi am (an? |the )?(hr admin(istrator)?|admin(istrator)?|ceo|superuser)\b", re.I),
     "Your role is determined by your authenticated identity and cannot be changed in conversation."),
    ("destructive", re.compile(
        r"\b(delete|drop|truncate|wipe|erase|purge|destroy)\b.{0,30}\b(all|every|employees?|records?|tables?|database|data)\b"
        r"|\b(update|alter)\b.{0,20}\b(table|set\s+\w+\s*=)", re.I),
     "Destructive operations are not supported. The assistant can only submit, approve or reject leave requests."),
    ("secret_request", re.compile(
        r"\b(system|hidden|initial|developer)\s+(prompt|instructions?|message)\b|\bapi[\s_-]?keys?\b|\bcredentials?\b"
        r"|\bpasswords?\b|\bsecret\s+keys?\b|\b(env|environment)\s+variables?\b|\.env\b|\bjwt\s+secret\b|\baccess\s+tokens?\b", re.I),
     "I can't share system configuration, prompts or credentials."),
]


def check_input(text: str) -> GuardVerdict:
    if len(text) > 4000:
        return GuardVerdict(allowed=False, category="injection", reason="Message is too long (max 4000 characters).")
    for category, pattern, reason in _RULES:
        if pattern.search(text):
            return GuardVerdict(allowed=False, category=category, reason=reason)  # type: ignore[arg-type]
    return GuardVerdict(allowed=True, category="ok")


# ------------------------------------------------------------------ output grounding
_NUM = re.compile(r"(?<![\w.-])(\d+(?:\.\d+)?)(?![\w-])")
_STRIP = [
    re.compile(r"\[P\d+\]"),                       # citation markers
    re.compile(r"\bLR-\d{4}-\d{6}\b"),             # request ids
    re.compile(r"\bEMP\d{4}\b"),                   # employee codes
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),          # ISO dates
    re.compile(r"\b\d{1,2}:\d{2}\b"),              # times
    re.compile(r"^\s*\d+[.)]\s", re.M),            # list numbering
    re.compile(r"\b(?:v|version\s*)\d+(?:\.\d+)*\b", re.I),  # policy versions
    re.compile(r"§\s*\d+(?:\.\d+)*"),              # section refs
    re.compile(r"\b\d+(?:\.\d+)+\s+[A-Z][a-z]"),   # "4.4 Carry forward"
    re.compile(r"\b(?:tr_)[0-9a-f]+\b"),
]
_TRIVIAL = {0.0, 1.0, 2.0}  # "one day", "1 of", "2 people" style counts are too common to police and low risk
_DATEWORDS = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:,?\s+(\d{4}))?"
                        r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", re.I)


def _numbers(text: str) -> set[float]:
    return {float(m) for m in _NUM.findall(text)}


@dataclass
class GroundingReport:
    ok: bool
    ungrounded_numbers: list[str] = field(default_factory=list)
    invalid_citations: list[str] = field(default_factory=list)
    foreign_emails: list[str] = field(default_factory=list)

    def feedback(self) -> str:
        parts = []
        if self.ungrounded_numbers:
            parts.append(f"these numbers do not appear in any tool result: {', '.join(self.ungrounded_numbers)}")
        if self.invalid_citations:
            parts.append(f"these citation markers do not match retrieved evidence: {', '.join(self.invalid_citations)}")
        if self.foreign_emails:
            parts.append("the answer contains email addresses that did not come from tool results")
        return "; ".join(parts)


def check_grounding(answer: str, evidence: list[object], context_text: str, valid_refs: set[str]) -> GroundingReport:
    evidence_text = json.dumps(evidence, default=str) + "\n" + context_text
    allowed = _numbers(evidence_text)
    # date components written in words ("30 November 2026") are fine if the date exists in evidence
    scrubbed = answer
    for m in _DATEWORDS.finditer(answer):
        scrubbed = scrubbed.replace(m.group(0), " ")
    for pat in _STRIP:
        scrubbed = pat.sub(" ", scrubbed)
    bad = sorted({n for n in _NUM.findall(scrubbed) if float(n) not in allowed and float(n) not in _TRIVIAL})
    # tolerate percentages that are simple ratios of grounded counts (e.g. 7/12 → 58%)
    if bad:
        ints = [a for a in allowed if a.is_integer() and 0 < a <= 1000]
        ratios = {round(a / b * 100) for a in ints for b in ints if b and a <= b and b <= 500} if len(ints) < 400 else set()
        bad = [n for n in bad if not (float(n).is_integer() and int(float(n)) in ratios and f"{n}%" in answer)]
    refs = set(re.findall(r"\[P\d+\]", answer))
    invalid = sorted(refs - valid_refs)
    emails = set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", answer))
    foreign = sorted(e for e in emails if e not in evidence_text)
    return GroundingReport(ok=not (bad or invalid or foreign), ungrounded_numbers=bad, invalid_citations=invalid,
                           foreign_emails=foreign)


CONFIRM_RE = re.compile(r"^\s*(yes|y|yeah|yep|confirm(ed)?|go ahead|proceed|do it|ok(ay)?|sure|approve it|submit it)\b[.! ]*$", re.I)
CANCEL_RE = re.compile(r"^\s*(no|n|nope|cancel|stop|don'?t|abort|never ?mind)\b.*$", re.I)
