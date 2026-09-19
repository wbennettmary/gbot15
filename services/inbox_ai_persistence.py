"""Persistence layer for AI Placement Optimization (Phase 2).

Stores AI jobs, suggestions, audit events, and prompt versions so AI
output is auditable and survives page refreshes. This module never
executes AI actions: it only records proposals and their lifecycle.
"""

import json
import uuid
from datetime import datetime, timedelta

JOB_TYPE_REGION_EDITOR = 'region_editor'
JOB_TYPE_TEMPLATE_ANALYSIS = 'template_analysis'
JOB_TYPE_MESSAGE_IMPROVE = 'message_improve'
JOB_TYPE_QUEUE_RECOMMENDATION = 'queue_recommendation'
JOB_TYPE_ALLOCATION_RECOMMENDATION = 'allocation_recommendation'
JOB_TYPE_READINESS_EXPLANATION = 'readiness_explanation'
JOB_TYPE_RESULT_ANALYSIS = 'result_analysis'

SUGGESTION_TYPE_REGION_TEXT = 'region_text'
SUGGESTION_TYPE_MESSAGE_HEADER = 'message_header'
SUGGESTION_TYPE_MESSAGE_BODY = 'message_body'
SUGGESTION_TYPE_QUEUE_CLEANUP = 'queue_cleanup'
SUGGESTION_TYPE_RESULT_ACTION = 'result_action'

JOB_STATUS_RUNNING = 'running'
JOB_STATUS_COMPLETED = 'completed'
JOB_STATUS_ERROR = 'error'

SUGGESTION_STATUS_PENDING = 'pending'
SUGGESTION_STATUS_ACCEPTED = 'accepted'
SUGGESTION_STATUS_REJECTED = 'rejected'
SUGGESTION_STATUS_EXPIRED = 'expired'

SUGGESTION_TTL_HOURS = 72

MAX_SUGGESTIONS_PER_JOB = 50


def generate_job_id():
    """Match the InboxPollJob id convention."""
    return uuid.uuid4().hex


def build_region_input_summary(payload):
    """Safe metadata-only summary of a region-editor request (no content)."""
    payload = payload or {}
    return {
        'region': (payload.get('region') or 'headline').strip().lower()[:50],
        'subject_chars': len(payload.get('subject') or ''),
        'html_chars': len(payload.get('html_body') or ''),
        'text_chars': len(payload.get('text_body') or ''),
        'custom_prompt_chars': len(payload.get('custom_prompt') or ''),
    }


def create_ai_job(job_type, *, job_id=None, test_id=None, source_template_id=None,
                  created_by=None, input_summary=None):
    """Create and commit a running job row. Returns the InboxAiJob."""
    from database import db, InboxAiJob
    if job_id:
        if InboxAiJob.query.filter_by(job_id=job_id).first():
            raise ValueError(f'AI job_id already exists: {job_id}')
    else:
        job_id = generate_job_id()
    summary_text = None
    if input_summary is not None:
        try:
            summary_text = json.dumps(input_summary)
        except (TypeError, ValueError):
            summary_text = None
    job = InboxAiJob(
        job_id=job_id,
        job_type=job_type,
        status=JOB_STATUS_RUNNING,
        test_id=test_id,
        source_template_id=source_template_id,
        created_by=created_by,
        input_summary_json=summary_text,
    )
    db.session.add(job)
    db.session.commit()
    return job


def complete_ai_job(job, *, provider, model=None, output=None):
    """Mark a job completed with its typed output (JSON-serialized)."""
    from database import db
    job.status = JOB_STATUS_COMPLETED
    job.provider = provider
    job.model = model
    job.completed_at = datetime.utcnow()
    if output is not None:
        try:
            job.output_json = json.dumps(output)
        except (TypeError, ValueError):
            job.output_json = None
    db.session.commit()


def fail_ai_job(job, error_message):
    """Mark a job errored with a safe message (no secrets in messages)."""
    from database import db
    job.status = JOB_STATUS_ERROR
    job.error_message = str(error_message or 'unknown error')[:2000]
    job.completed_at = datetime.utcnow()
    db.session.commit()


def persist_suggestions(job, suggestions, *, suggestion_type='region_text', target_region=None):
    """Store provider suggestions as pending InboxAiSuggestion rows.

    Returns the number of rows created. Content is stored exactly as the
    frontend contract for the suggestion type (e.g. {text, rationale,
    expected_lift} for region text) so later phases can render it.
    """
    from database import db, InboxAiSuggestion
    created = 0
    for item in (suggestions or [])[:MAX_SUGGESTIONS_PER_JOB]:
        if not isinstance(item, dict) or not item.get('text'):
            continue
        content = {key: str(item.get(key) or '') for key in item if key != 'placeholder_report'}
        try:
            content_text = json.dumps(content)
        except (TypeError, ValueError):
            continue
        row = InboxAiSuggestion(
            job_id=job.job_id,
            test_id=job.test_id,
            suggestion_type=suggestion_type,
            target_region=(target_region or '')[:50] or None,
            content_json=content_text,
            status=SUGGESTION_STATUS_PENDING,
        )
        db.session.add(row)
        created += 1
    if created:
        db.session.commit()
    return created


def persist_region_suggestions(job, suggestions, region):
    """Backward-compatible wrapper for region editor suggestions."""
    return persist_suggestions(job, suggestions, suggestion_type='region_text', target_region=region)


def build_message_input_summary(payload, target=None):
    """Safe metadata-only summary of a Message Setup AI request."""
    payload = payload or {}
    return {
        'target': (target or payload.get('target') or '')[:20],
        'body_mode': (payload.get('body_mode') or 'html')[:10],
        'header_chars': len(payload.get('header') or ''),
        'body_chars': len(payload.get('body') or ''),
        'custom_prompt_chars': len(payload.get('custom_prompt') or ''),
    }


def record_audit_event(event_type, *, job_id=None, test_id=None, suggestion_id=None,
                       user_email=None, payload=None):
    """Append one audit event; failures must never break the main flow."""
    from database import db, InboxAiAuditEvent
    try:
        payload_text = None
        if payload is not None:
            try:
                payload_text = json.dumps(payload)
            except (TypeError, ValueError):
                payload_text = None
        db.session.add(InboxAiAuditEvent(
            event_type=event_type,
            job_id=job_id,
            test_id=test_id,
            suggestion_id=suggestion_id,
            user_email=user_email,
            payload_json=payload_text,
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def expire_stale_suggestions(job_id=None):
    """Mark pending suggestions older than the TTL as expired.

    Keeps accepted/rejected/expired distinguishable without a cron job.
    Returns the number of rows updated.
    """
    from database import db, InboxAiSuggestion
    cutoff = datetime.utcnow() - timedelta(hours=SUGGESTION_TTL_HOURS)
    query = InboxAiSuggestion.query.filter(
        InboxAiSuggestion.status == SUGGESTION_STATUS_PENDING,
        InboxAiSuggestion.created_at < cutoff,
    )
    if job_id:
        query = query.filter(InboxAiSuggestion.job_id == job_id)
    stale = query.all()
    for row in stale:
        row.status = SUGGESTION_STATUS_EXPIRED
    if stale:
        db.session.commit()
    return len(stale)


def serialize_job(job):
    return {
        'job_id': job.job_id,
        'job_type': job.job_type,
        'status': job.status,
        'test_id': job.test_id,
        'provider': job.provider,
        'model': job.model,
        'input_summary': json.loads(job.input_summary_json) if job.input_summary_json else {},
        'output': json.loads(job.output_json) if job.output_json else None,
        'error_message': job.error_message,
        'created_at': job.created_at.isoformat() + 'Z' if job.created_at else None,
        'completed_at': job.completed_at.isoformat() + 'Z' if job.completed_at else None,
    }


def serialize_suggestion(row):
    try:
        content = json.loads(row.content_json)
    except (TypeError, ValueError):
        content = {}
    return {
        'id': row.id,
        'job_id': row.job_id,
        'suggestion_type': row.suggestion_type,
        'target_region': row.target_region,
        'status': row.status,
        'confidence': row.confidence,
        'content': content,
        'created_at': row.created_at.isoformat() + 'Z' if row.created_at else None,
        'decided_at': row.decided_at.isoformat() + 'Z' if row.decided_at else None,
    }


def get_job_payload(job_id):
    """Full polling payload for GET /ai/jobs/<job_id>, or None if unknown."""
    from database import InboxAiJob, InboxAiSuggestion
    job = InboxAiJob.query.filter_by(job_id=job_id).first()
    if not job:
        return None
    expire_stale_suggestions(job_id=job_id)
    suggestions = (InboxAiSuggestion.query.filter_by(job_id=job_id)
                   .order_by(InboxAiSuggestion.id.asc()).all())
    return {
        'job': serialize_job(job),
        'suggestions': [serialize_suggestion(s) for s in suggestions],
    }


def record_prompt_version(prompt_key, prompt_text, updated_by=None):
    """Insert a new prompt version when the text changed. Returns version no."""
    from database import db, InboxAiPromptVersion
    latest = (InboxAiPromptVersion.query.filter_by(prompt_key=prompt_key)
              .order_by(InboxAiPromptVersion.version.desc()).first())
    if latest is not None and (latest.prompt_text or '') == (prompt_text or ''):
        return latest.version
    version = (latest.version + 1) if latest is not None else 1
    db.session.add(InboxAiPromptVersion(
        prompt_key=prompt_key,
        prompt_text=prompt_text or '',
        version=version,
        updated_by=updated_by,
    ))
    db.session.commit()
    return version
