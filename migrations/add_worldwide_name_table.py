"""Create the global Inbox Intelligence name-reservation table.

The application also creates this table through ``db.create_all()`` on startup;
this targeted migration is provided for explicit deployment runs.
"""

import os
import sys

from sqlalchemy import create_engine, inspect, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import SQLALCHEMY_DATABASE_URI
from database import InboxWorldwideName


def migrate_worldwide_name_table():
    engine = create_engine(SQLALCHEMY_DATABASE_URI)
    InboxWorldwideName.metadata.create_all(bind=engine, tables=[InboxWorldwideName.__table__], checkfirst=True)
    inspector = inspect(engine)
    columns = [column['name'] for column in inspector.get_columns('inbox_worldwide_name')]
    new_columns = {
        'generation_batch_id': 'VARCHAR(36)',
        'used_at': 'TIMESTAMP',
        'used_by': 'VARCHAR(80)',
        'used_for': 'VARCHAR(255)',
        'archived_at': 'TIMESTAMP',
        'archived_by': 'VARCHAR(255)',
    }
    with engine.begin() as connection:
        for column_name, column_type in new_columns.items():
            if column_name not in columns:
                connection.execute(text(
                    f'ALTER TABLE inbox_worldwide_name ADD COLUMN {column_name} {column_type}'
                ))
        indexes = {
            index['name']
            for index in inspect(engine).get_indexes('inbox_worldwide_name')
        }
        if 'ix_inbox_worldwide_name_generation_batch_id' not in indexes:
            connection.execute(text(
                'CREATE INDEX ix_inbox_worldwide_name_generation_batch_id '
                'ON inbox_worldwide_name (generation_batch_id)'
            ))
        if 'ix_inbox_worldwide_name_archived_at' not in indexes:
            connection.execute(text(
                'CREATE INDEX ix_inbox_worldwide_name_archived_at '
                'ON inbox_worldwide_name (archived_at)'
            ))
    print('Worldwide name reservation table is ready.')


if __name__ == '__main__':
    migrate_worldwide_name_table()
