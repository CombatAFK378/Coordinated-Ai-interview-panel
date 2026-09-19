"""Deterministic addressee routing shared by spoken and typed panel questions."""
import re

from shared import prompts


def contains_phrase(text, phrase):
    return re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text, re.I) is not None


class PanelRouter:
    ALIASES = {
        "orchestrator": ("julian", "orchestrator", "host", "whole panel", "everyone"),
        "hiring_manager": ("hiring manager", "hr", "dm"),
        "technical": ("technical interviewer", "technical", "tech interviewer"),
        "product": ("product interviewer", "product manager"),
        "customer": ("customer advocate",),
        "coding": ("coding interviewer",),
    }
    OPEN = ("overall", "recommendation", "the panel", "final decision", "conclusion",
            "override", "overrule", "accept", "reject", "decline", "proceed", "hire")

    def __init__(self):
        self.last_target = None

    @classmethod
    def detect(cls, text):
        # Personal names take precedence over topics mentioned in a question.
        for aid, agent in prompts.AGENTS.items():
            if contains_phrase(text, agent.name):
                return aid
        for aid, aliases in cls.ALIASES.items():
            if any(contains_phrase(text, alias) for alias in aliases):
                return aid
        return None

    def resolve(self, text):
        named = self.detect(text)
        if named:
            self.last_target = None if named == "orchestrator" else named
        elif any(contains_phrase(text, phrase) for phrase in self.OPEN):
            self.last_target = None
        return self.last_target
