from app import app, db
from sqlalchemy import text
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def migrate():
    with app.app_context():
        logger.info("Starting database migration for Multi-Account Support...")
        
        inspector = db.inspect(db.engine)
        
        # 1. Update AwsConfig table
        columns_aws = [col['name'] for col in inspector.get_columns('aws_config')]
        
        if 'name' not in columns_aws:
            logger.info("Adding 'name' column to 'aws_config' table...")
            with db.engine.connect() as conn:
                conn.execute(text('ALTER TABLE aws_config ADD COLUMN name VARCHAR(255) DEFAULT "Default Account"'))
                conn.commit()
            logger.info("Added 'name' column.")
        else:
            logger.info("'name' column already exists in 'aws_config'.")

        # 2. Update User table
        columns_user = [col['name'] for col in inspector.get_columns('user')]
        
        if 'active_aws_config_id' not in columns_user:
            logger.info("Adding 'active_aws_config_id' column to 'user' table...")
            with db.engine.connect() as conn:
                conn.execute(text('ALTER TABLE user ADD COLUMN active_aws_config_id INTEGER REFERENCES aws_config(id)'))
                conn.commit()
            logger.info("Added 'active_aws_config_id' column.")
        else:
            logger.info("'active_aws_config_id' column already exists in 'user'.")

        logger.info("Migration completed successfully.")

if __name__ == "__main__":
    migrate()
