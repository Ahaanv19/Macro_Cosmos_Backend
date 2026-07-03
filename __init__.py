from flask import Flask
from flask_login import LoginManager
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from dotenv import load_dotenv
import os

# Load environment variables from .env file
load_dotenv()

# Setup of key Flask object (app)
app = Flask(__name__)

# Initialize Flask-Login object
login_manager = LoginManager()
login_manager.init_app(app)

# Allowed servers for cross-origin resource sharing (CORS), these are GitHub Pages and localhost for GitHub Pages testing
cors = CORS(app, 
    supports_credentials=True, 
    origins=[
        'http://localhost:4887', 
        'http://127.0.0.1:4887', 
        'http://localhost:4100',
        'http://127.0.0.1:4100',
        'https://ahaanv19.github.io',
        'https://macro-cosmos.netlify.app',
        # Netlify deploy/branch previews (anchored regex — flask-cors treats
        # patterns containing regex metachars as regular expressions).
        r'^https://([a-z0-9-]+--)?macro-cosmos\.netlify\.app$',
    ],
    allow_headers=['Content-Type', 'Authorization', 'X-Origin'],
    methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS']
)

# System Defaults
app.config['ADMIN_USER'] = os.environ.get('ADMIN_USER') or 'admin'
app.config['ADMIN_PASSWORD'] = os.environ.get('ADMIN_PASSWORD') or os.environ.get('DEFAULT_PASSWORD') or 'password'
app.config['DEFAULT_USER'] = os.environ.get('DEFAULT_USER') or 'user'
app.config['DEFAULT_PASSWORD'] = os.environ.get('DEFAULT_PASSWORD') or 'password'

# Browser settings
SECRET_KEY = os.environ.get('SECRET_KEY') or 'SECRET_KEY' # secret key for session management
SESSION_COOKIE_NAME = os.environ.get('SESSION_COOKIE_NAME') or 'sess_python_flask'
JWT_TOKEN_NAME = os.environ.get('JWT_TOKEN_NAME') or 'jwt_python_flask'
app.config['SECRET_KEY'] = SECRET_KEY
app.config['SESSION_COOKIE_NAME'] = SESSION_COOKIE_NAME 
app.config['JWT_TOKEN_NAME'] = JWT_TOKEN_NAME 

# Database settings 
dbName = 'user_management'
DB_ENDPOINT = os.environ.get('DB_ENDPOINT') or None
DB_USERNAME = os.environ.get('DB_USERNAME') or None
DB_PASSWORD = os.environ.get('DB_PASSWORD') or None
if DB_ENDPOINT and DB_USERNAME and DB_PASSWORD:
    # Production - Use MySQL
    
    DB_PORT = '3306'
    DB_NAME = dbName
    dbString = f'mysql+pymysql://{DB_USERNAME}:{DB_PASSWORD}@{DB_ENDPOINT}:{DB_PORT}'
    dbURI =  dbString + '/' + dbName
    backupURI = None  # MySQL backup would require a different approach
else:
    # Development - Use SQLite
    dbString = 'sqlite:///volumes/'
    dbURI = dbString + dbName + '.db'
    backupURI = dbString + dbName + '_bak.db'

app.config['DB_ENDPOINT'] = DB_ENDPOINT
app.config['DB_USERNAME'] = DB_USERNAME
app.config['DB_PASSWORD'] = DB_PASSWORD
app.config['SQLALCHEMY_DATABASE_NAME'] = dbName
app.config['SQLALCHEMY_DATABASE_STRING'] = dbString
app.config['SQLALCHEMY_DATABASE_URI'] = dbURI
app.config['SQLALCHEMY_BACKUP_URI'] = backupURI
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
migrate = Migrate(app, db)

# Image upload settings 
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # maximum size of uploaded content
app.config['UPLOAD_EXTENSIONS'] = ['.jpg', '.png', '.gif']  # supported file types
app.config['UPLOAD_FOLDER'] = os.path.join(app.instance_path, 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# GITHUB settings
app.config['GITHUB_API_URL'] = 'https://api.github.com'
app.config['GITHUB_TOKEN'] = os.environ.get('GITHUB_TOKEN') or None
app.config['GITHUB_TARGET_TYPE'] = os.environ.get('GITHUB_TARGET_TYPE') or 'user'
app.config['GITHUB_TARGET_NAME'] = os.environ.get('GITHUB_TARGET_NAME') or 'nighthawkcoders'

# KASM settings
app.config['KASM_SERVER'] = os.environ.get('KASM_SERVER') or 'https://kasm.nighthawkcodingsociety.com'
app.config['KASM_API_KEY'] = os.environ.get('KASM_API_KEY') or None
app.config['KASM_API_KEY_SECRET'] = os.environ.get('KASM_API_KEY_SECRET') or None

# Security hardening: rate limiting, security/CSP headers, audit logging,
# secure cookies (prod), and weak-secret checks. Safe-by-default — see
# utils/security.py. Wired last so it can read the config set above.
# Wrapped defensively: if a security dependency is missing in an environment,
# the app still boots (degraded) rather than returning 502, and logs loudly.
try:
    from utils.security import init_security
    init_security(app)
except Exception as _sec_err:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger(__name__).error(
        "Security hardening failed to initialize (%s). The app is running "
        "WITHOUT the new security middleware. Check that Flask-Limiter and "
        "bleach are installed (pip install -r requirements.txt).", _sec_err,
    )