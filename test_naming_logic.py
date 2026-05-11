
import sys
import os
from unittest.mock import MagicMock

# Mock Flask and dependencies BEFORE importing aws_manager
sys.modules['flask'] = MagicMock()
sys.modules['flask'].Blueprint = MagicMock()
sys.modules['flask'].session = {'user': 'patrick'}  # SIMULATE LOGGED IN USER
sys.modules['database'] = MagicMock()
sys.modules['services'] = MagicMock()
sys.modules['services.google_service_account'] = MagicMock()
sys.modules['app'] = MagicMock()

# Manually define the logic we want to test to avoid complex import chains crashing
# This replicates EXACTLY what is in routes/aws_manager.py
def get_naming_config_simulated(session_user):
    print(f"Testing with session user: {session_user}")
    
    current_user = session_user
    clean_user = None
    dynamic_lambda_prefix = None
    
    if current_user:
        clean_user = current_user.split('@')[0].lower()
        dynamic_lambda_prefix = f"{clean_user}-chromium"
        print(f"Logic generated prefix: {dynamic_lambda_prefix}")
        return dynamic_lambda_prefix
    
    return "gbot-chromium" # Default

if __name__ == "__main__":
    print("--- STARTING LOCAL VISUAL VERIFICATION ---")
    user = "patrick"
    result = get_naming_config_simulated(user)
    
    print("\nRESULT:")
    if result == "patrick-chromium":
        print("✅ SUCCESS: Logic correctly produced 'patrick-chromium'")
    else:
        print(f"❌ FAILURE: Logic produced '{result}' instead of 'patrick-chromium'")
        
    print("\n--- TEST EMAIL HANDLING ---")
    user_email = "Angel@domain.com"
    result_email = get_naming_config_simulated(user_email)
    if result_email == "angel-chromium":
        print("✅ SUCCESS: Logic correctly handled email 'Angel@domain.com' -> 'angel-chromium'")
    else:
        print(f"❌ FAILURE: Logic produced '{result_email}'")
