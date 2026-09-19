"""
Database migration script for AI Placement Optimization (Phase 2).
Creates the inbox_ai_job, inbox_ai_suggestion, inbox_ai_audit_event,
and inbox_ai_prompt_version tables.

Idempotent: existing tables are left untouched (checkfirst=True).
The app also creates these automatically via db.create_all() at startup;
this script exists for ops parity with the other migrations.
"""
import os
import sys
from sqlalchemy import create_engine

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import SQLALCHEMY_DATABASE_URI
from database import db, InboxAiJob, InboxAiSuggestion, InboxAiAuditEvent, InboxAiPromptVersion

def migrate_inbox_ai_tables():
    """Create the AI job/suggestion/audit/prompt-version tables."""
    try:
        engine = create_engine(SQLALCHEMY_DATABASE_URI)
        print("Starting AI Placement Optimization tables migration...")
        db.metadata.create_all(
            bind=engine,
            tables=[
                InboxAiJob.__table__,
                InboxAiSuggestion.__table__,
                InboxAiAuditEvent.__table__,
                InboxAiPromptVersion.__table__,
            ],
            checkfirst=True,
        )
        print("Created inbox_ai_job table")
        print("Created inbox_ai_suggestion table")
        print("Created inbox_ai_audit_event table")
        print("Created inbox_ai_prompt_version table")
        print("AI Placement Optimization tables migration completed successfully!")
        return True
    except Exception as e:
        print(f"Migration failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == '__main__':
    print("AI Placement Optimization Database Migration Tool")
    print("=" * 50)
    success = migrate_inbox_ai_tables()
    if success:
        print("\nMigration completed successfully!")
        sys.exit(0)
    else:
        print("\nMigration failed!")
        sys.exit(1)
