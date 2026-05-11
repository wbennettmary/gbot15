
import os
import sys

# Set dummy environment variables to avoid app startup errors
os.environ['SECRET_KEY'] = 'dummy'
os.environ['WHITELIST_TOKEN'] = 'dummy'
# os.environ['FLASK_ENV'] = 'development' # Let's NOT set this to see what the default is

# Add the project directory to sys.path
sys.path.append(os.getcwd())

from app import app
with app.app_context():
    uri = app.config['SQLALCHEMY_DATABASE_URI']
    print(f"DATABASE URI: {uri}")
    if uri.startswith('sqlite:///'):
        path = uri.replace('sqlite:///', '')
        import os
        if os.path.isabs(path):
            abs_path = path
        else:
            abs_path = os.path.abspath(path)
        print(f"ABS PATH: {abs_path}")
        if os.path.exists(abs_path):
            print(f"EXISTS: Yes, size: {os.path.getsize(abs_path)} bytes")
        else:
            print("EXISTS: No")
