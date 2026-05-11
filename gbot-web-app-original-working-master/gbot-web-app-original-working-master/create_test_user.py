
from app import app, db, User
from werkzeug.security import generate_password_hash

with app.app_context():
    # Check if user exists
    user = User.query.filter_by(username='testuser').first()
    if not user:
        print("Creating test user...")
        # Assuming User model has username, password_hash, role
        # Try to inspect model fields if needed, but standard guess first
        try:
            # DIRECT HASH ASSIGNMENT TO 'password' FIELD
            hashed_pw = generate_password_hash('password')
            user = User(username='testuser', role='admin', password=hashed_pw)
            db.session.add(user)
            db.session.commit()
            print("User created successfully with direct password assignment.")
        except Exception as e:
            print(f"Failed to create user: {e}")
    else:
        print("Test user already exists. Updating password...")
        user.set_password('password')
        db.session.commit()
        print("Password updated.")
