
import os
from flask import Flask
from config import SQLALCHEMY_DATABASE_URI, SQLALCHEMY_ENGINE_OPTIONS

print(f"--- DEBUG CONFIG ---")
print(f"FLASK_ENV: {os.environ.get('FLASK_ENV')}")
print(f"DATABASE_URL env: {os.environ.get('DATABASE_URL')}")
print(f"Calculated SQLALCHEMY_DATABASE_URI: {SQLALCHEMY_DATABASE_URI}")
print(f"Is SQLite: {'sqlite' in SQLALCHEMY_DATABASE_URI}")
print(f"Is PostgreSQL: {'postgresql' in SQLALCHEMY_DATABASE_URI}")
print(f"--------------------")
