"""The fixed prompts and recommendation reasons of the side-channel's questions (#223): every
question the engine asks the author carries a recommended option and why (owner decision 4,
2026-10-10). Prompts name entities, records and times; they never quote transcript text."""

from __future__ import annotations

ENTITY_PROMPT = "Which {kind} do you mean by “{mention}”?"
ENTITY_REASON = "“{name}” is the closest match to “{mention}”."

PREDICATE_PROMPT = "“{predicate}” is not a relation this story knows. Which one do you mean?"
PREDICATE_REASON = "“{name}” is the closest registered relation."
REGISTER_LABEL = "Register “{predicate}” as a new relation"

TIME_PROMPT = "When did “{change}” begin? “{expression}” is not a time the clock can place."
TIME_BEFORE_LABEL = "Before the story began"
TIME_NOW_LABEL = "Now (the story's current time)"
TIME_BEFORE_REASON = "“{expression}” names a time before the story's events."
TIME_NOW_REASON = "The story's current time is the latest time the change can begin."

RECORD_PROMPT = "Which line do you mean by “{quote}”? It matches {count} lines."
RECORD_REASON = "The most recent matching line is the likeliest one."

STATE_PROMPT = "“{state}” is computed from other values. Which value do you mean to change?"
STATE_REASON = "“{name}” is the value “{state}” is computed from."

CONFLICT_PROMPT = (
    "A record ({table}) contradicts the change and cannot be repaired by substitution "
    "({reason}). What should happen to it?"
)
CONFLICT_REASONS = {
    "rewrite": "Rewriting the line to agree is the smallest change that keeps it.",
    "drop": "Removing it is the smallest change that keeps the story consistent.",
    "cascade_undo": "The amendment it rests on no longer holds, so undoing it is the least change.",
    "keep": "Keeping it leaves the story inconsistent.",
}

MODEL_REASON = "The parser recommended this reading."
MODEL_FALLBACK_REASON = "The first reading is the one the instruction most directly states."

PAST_LIFE_STAGES = (
    "childhood",
    "child",
    "kid",
    "school",
    "college",
    "university",
    "youth",
    "young",
    "teen",
    "born",
    "birth",
    "years ago",
    "before",
    "always",
    "originally",
)
"""Words that make an unplaceable time expression a past life stage: the recommendation is
then "before the story began" (#223's recommendation rule)."""

ABSOLUTE_PROMPT = (
    "The story's clock cannot tell whether {expression} came before or after parts of the "
    "story. Where does this correction belong?"
)
"""#240: the ``timing`` question for an undecidable absolute time (``ask_author``)."""
ABSOLUTE_REASONS = {
    0: "A year or a month this broad most likely lies before the story began.",
    1: "Placed before the latest turn.",
    2: "A date this exact most likely belongs to the story's recent past: after the latest turn.",
}


__all__ = [
    "ABSOLUTE_PROMPT",
    "ABSOLUTE_REASONS",
    "CONFLICT_PROMPT",
    "CONFLICT_REASONS",
    "ENTITY_PROMPT",
    "ENTITY_REASON",
    "MODEL_FALLBACK_REASON",
    "MODEL_REASON",
    "PAST_LIFE_STAGES",
    "PREDICATE_PROMPT",
    "PREDICATE_REASON",
    "RECORD_PROMPT",
    "RECORD_REASON",
    "REGISTER_LABEL",
    "STATE_PROMPT",
    "STATE_REASON",
    "TIME_BEFORE_LABEL",
    "TIME_BEFORE_REASON",
    "TIME_NOW_LABEL",
    "TIME_NOW_REASON",
    "TIME_PROMPT",
]
