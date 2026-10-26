"""Export real source inputs for trusted OpenClaw jobs; import matching results.

No subprocess, CLI, RPC or fabricated model output. Parent owns inference.
"""
from __future__ import annotations
import copy
import json
from dataclasses import fields
from pathlib import Path

from .luna import LunaError, _apply, input_hash, messages_for
from .parse import ContestCard
from .store import get_contest, stable_id, upsert_contests


def _card(value):
    return value if isinstance(value, ContestCard) else ContestCard(**{
        key: val for key, val in value.items() if key in {f.name for f in fields(ContestCard)}})


def export_jobs(cards, path=None, *, model="openai/gpt-6.1-sol", include_verified=False):
    jobs = []
    for value in cards:
        card = _card(value)
        if card.curated or card.record_origin == "curated":
            continue
        if not include_verified and card.luna_state == "verified" and card.luna_input_hash == input_hash(card):
            continue
        jobs.append({"id": stable_id(card), "input_hash": input_hash(card),
                     "model": model, "messages": messages_for(card)})
    bundle = {"schema": "saishi-luna-jobs-v1", "jobs": jobs}
    if path is not None:
        Path(path).write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return bundle


def import_results(bundle, *, lookup=None, upsert=None):
    """Trusted operator boundary; strict input hash/identity/curation check.

    Bundle: {schema:'saishi-luna-results-v1', results:[{id,input_hash,model,result}]}.
    Model provenance is operator attestation, not a cryptographic proof.
    Entire batch validates before any write. Curated/stale/duplicate results reject.
    """
    if isinstance(bundle, (str, Path)):
        bundle = json.loads(Path(bundle).read_text(encoding="utf-8"))
    if not isinstance(bundle, dict) or set(bundle) != {"schema", "results"} or bundle["schema"] != "saishi-luna-results-v1" or not isinstance(bundle["results"], list) or len(bundle["results"]) > 1000:
        raise LunaError("invalid_job_bundle")
    lookup = lookup or (lambda ident: get_contest(ident, include_drafts=True))
    upsert = upsert or upsert_contests
    pending, seen = [], set()
    for job in bundle["results"]:
        if not isinstance(job, dict) or set(job) != {"id", "input_hash", "model", "result"} or not isinstance(job["id"], str) or job["id"] in seen:
            raise LunaError("invalid_job")
        seen.add(job["id"])
        current = lookup(job["id"])
        if not current:
            raise LunaError("job_record_missing")
        card = copy.deepcopy(_card(current))
        if card.curated or card.record_origin == "curated":
            raise LunaError("job_curated_record")
        if stable_id(card) != job["id"] or input_hash(card) != job["input_hash"]:
            raise LunaError("job_input_changed")
        pending.append(_apply(card, job["result"], job["model"]))
    if pending:
        upsert(pending)
    return {"imported": len(pending), "ids": [c.id or stable_id(c) for c in pending]}
