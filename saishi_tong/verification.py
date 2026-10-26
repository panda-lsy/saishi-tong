"""Independent AI and administrator review; never fabricate model evidence."""
import hashlib
import json
from dataclasses import asdict, is_dataclass

VERIFIED_SQL = "(human_reviewed_at != '')"
AI_REVIEWED_SQL = "(luna_state = 'verified' OR ai_reviewed_at != '')"
REVIEW_GROUP_SQL = "CASE WHEN " + VERIFIED_SQL + " THEN 'verified' WHEN " + AI_REVIEWED_SQL + " THEN 'awaiting' ELSE 'unverified' END"


def values(card):
    return asdict(card) if is_dataclass(card) else card


def facts_hash(card, *, include_location=True):
    from .luna import FACT_FIELDS
    data = values(card)
    facts = {key: data.get(key, '') for key in (*FACT_FIELDS, 'source_url') if include_location or key != 'location_scope'}
    return hashlib.sha256(json.dumps(facts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def human_reviewed(card):
    data = values(card)
    return bool(data.get('human_reviewed_at') and data.get('human_reviewed_hash') == facts_hash(data))


def is_verified(card):
    return human_reviewed(card)


def describe(card):
    data = dict(values(card))
    ai, human = data.get('luna_state') == 'verified' or bool(data.get('ai_reviewed_at')), human_reviewed(data)
    state = 'ai_manual' if ai and human else 'manual' if human else 'ai' if ai else 'unverified'
    data.update(verification_state=state, human_reviewed=human,
                verification_label={'ai_manual': 'AI核验＋人工校对', 'manual': '人工校对',
                                    'ai': 'AI核验完成·待人工确认', 'unverified': '未核验'}[state])
    return data
