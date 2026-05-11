from app import app, db
from database import UsedDomain
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def clear_domains():
    with app.app_context():
        try:
            # Count before deletion
            count = UsedDomain.query.count()
            logger.info(f"Found {count} domains to delete.")
            
            if count > 0:
                # Delete all rows
                num_deleted = db.session.query(UsedDomain).delete()
                db.session.commit()
                logger.info(f"Successfully deleted {num_deleted} domains from the database.")
            else:
                logger.info("No domains to delete.")
                
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error deleting domains: {e}")

if __name__ == "__main__":
    clear_domains()
