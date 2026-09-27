#!/usr/bin/env python3
"""Importance scoring for memories and facts using a small local model.

The model rates a piece of text 1..10 for long-term relevance. The score
gates what the memory keeps:
  >= 5  -> worth embedding (vector memory)
  >= 8  -> worth storing as an explicit fact
On any error the neutral score 5.0 is returned, so ingestion never breaks.

Usage (as a module):
  from importance import score_chunk, should_embed, should_store_as_fact
Usage (CLI, for a quick check):
  importance.py "I prefer answers in German." [--context "..."]

Environment: LLM_BASE_URL, LLM_MODEL (see memdb.py).

Author: Lukas Weißmann
License: MIT
"""
import argparse

from memdb import chat

SCORE_MIN_EMBEDDING = 5.0
SCORE_MIN_FACT      = 8.0

_PROMPT = """You rate how important a piece of information is for a long-term memory.
Scale 1-10:
 1-3  = trivial, forgotten after minutes (small talk, filler)
 4-6  = useful context, relevant in the medium term
 7-9  = important insight, preference or fact
 10   = critical (core preference, hard constraint, long-term commitment)

Answer ONLY with a single number (e.g. "7"). No explanation.

Conversation context:
{context}

Information to rate:
{text}
"""


def score_chunk(text: str, context: str = "") -> float:
    """Return importance score 1.0-10.0 for text. Falls back to 5.0 on error."""
    prompt = _PROMPT.format(context=context[:500], text=text[:800])
    try:
        raw = chat(prompt, max_tokens=8, temperature=0.1, timeout=30)
        # Take the first number in the answer
        for tok in raw.replace(",", ".").split():
            try:
                val = float(tok)
                return max(1.0, min(10.0, val))
            except ValueError:
                continue
    except Exception:
        pass
    return 5.0


def should_embed(score: float) -> bool:
    return score >= SCORE_MIN_EMBEDDING


def should_store_as_fact(score: float) -> bool:
    return score >= SCORE_MIN_FACT


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text")
    ap.add_argument("--context", default="")
    a = ap.parse_args()
    s = score_chunk(a.text, a.context)
    print(f"score={s:.1f} embed={should_embed(s)} fact={should_store_as_fact(s)}")
