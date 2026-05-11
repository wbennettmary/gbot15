
import sys
from unittest.mock import MagicMock

# 1. Mock EVERYTHING before importing the logic
sys.modules['flask'] = MagicMock()
sys.modules['flask'].session = {'user_id': 123} # SIMULATE OLD SESSION (Missing 'user')
sys.modules['database'] = MagicMock()
sys.modules['app'] = MagicMock()

# Mock DB User Query
mock_user = MagicMock()
mock_user.username = "RecoveredUser"
sys.modules['database'].User.query.get.return_value = mock_user

# 2. Define the exact logic we inserted into aws_manager.py
def get_lambda_prefix_logic():
    session = sys.modules['flask'].session
    config_lambda_prefix = "db-default"
    DEFAULT_PRODUCTION_LAMBDA_NAME = "default-chromium"
    
    # The COMPLEX ONE-LINER from aws_manager.py
    return (
        f"{(session.get('user') or (lambda: (__import__('database').User.query.get(session['user_id']).username if session.get('user_id') else None))()).split('@')[0].lower()}-chromium"
        if (session.get('user') or session.get('user_id')) 
        else (config_lambda_prefix or DEFAULT_PRODUCTION_LAMBDA_NAME)
    )

if __name__ == "__main__":
    print("--- TESTING SESSION REPAIR LOGIC ---")
    
    # CASE 1: Session has user_id but NO user
    print("\nCase 1: Old Session (user_id only)")
    prefix = get_lambda_prefix_logic()
    print(f"Generated Prefix: {prefix}")
    
    if prefix == "recovereduser-chromium":
        print("✅ SUCCESS: Logic recovered username from DB!")
    else:
        print(f"❌ FAILURE: Expected 'recovereduser-chromium', got '{prefix}'")

    # CASE 2: Session has BOTH (New Session)
    print("\nCase 2: New Session (user + user_id)")
    sys.modules['flask'].session['user'] = "NewGuy"
    prefix_new = get_lambda_prefix_logic()
    print(f"Generated Prefix: {prefix_new}")
    
    if prefix_new == "newguy-chromium":
        print("✅ SUCCESS: Logic used session username directly.")
    else:
        print(f"❌ FAILURE: Expected 'newguy-chromium', got '{prefix_new}'")
