"""Loads the knowledge pack and the scoring checks, and turns them into prompt text."""
import json
import os

from . import CONFIG, KNOWLEDGE


def load_knowledge(path=None):
    with open(path or os.path.join(KNOWLEDGE, "knowledge.json"), encoding="utf-8") as f:
        return json.load(f)


def load_rules(path=None):
    with open(path or os.path.join(CONFIG, "rules.json"), encoding="utf-8") as f:
        return json.load(f)


def guardrail_ids(k):
    return [g["id"] for g in k.get("guardrails", [])]


def unvetted(k):
    return [g["id"] for g in k.get("guardrails", []) if g.get("status") == "to_be_vetted"]


def knowledge_text(k, vertical=None):
    L = ["KNOWLEDGE PACK (the only source for Vi guidance and platform behaviour; cite guardrail IDs)", ""]
    L.append("Vi guardrails:")
    for g in k.get("guardrails", []):
        L.append(f"- {g['id']}: {g['text']}")
    L.append("\nVerified platform facts:")
    for p in k.get("platform_facts", []):
        L.append(f"- {p['id']}: {p['text']}")
    L.append("\nDo NOT assert:")
    for d in k.get("do_not_assert", []):
        L.append(f"- {d}")
    if vertical:
        L.append(f"\nThis brand's vertical: {vertical}. Apply the industry guardrails that match it, and say so.")
    return "\n".join(L)
