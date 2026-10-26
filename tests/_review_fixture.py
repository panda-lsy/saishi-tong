"""Explicit human-approved synthetic fixtures for public-page regressions."""
from saishi_tong.classify import enrich_card
from saishi_tong.verification import facts_hash


def reviewed(card):
    enrich_card(card)
    card.human_reviewed_at = '2026-10-23T00:00:00+08:00'
    card.human_reviewed_hash = facts_hash(card)
    return card
