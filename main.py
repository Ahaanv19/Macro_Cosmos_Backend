# imports from flask
import json
import os
from urllib.parse import urljoin, urlparse
from flask import abort, redirect, render_template, request, send_from_directory, url_for, jsonify
from flask_login import current_user, login_user, logout_user
from flask.cli import AppGroup
from flask_login import current_user, login_required
from flask import current_app
from werkzeug.security import generate_password_hash
import shutil
from flask import Flask
from flask_cors import CORS 
import logging

# import "objects" from "this" project
from __init__ import app, db, login_manager
# Security helpers: RBAC, IP allowlist, audit logging, rate limiting.
# Imported defensively: if a security dependency is missing in some environment,
# fall back to no-op shims so the app still boots (degraded) instead of 502.
try:
    from utils.security import admin_required, ip_allowlist, audit, limiter
except Exception as _sec_err:  # noqa: BLE001
    logging.getLogger(__name__).error(
        "Security helpers unavailable (%s); running WITHOUT RBAC/rate-limit "
        "decorators. Install deps: pip install -r requirements.txt", _sec_err,
    )

    def admin_required(func):
        return func

    def ip_allowlist(*_args, **_kwargs):
        def decorator(func):
            return func
        return decorator

    def audit(*_args, **_kwargs):
        return None

    class _NoopLimiter:
        def limit(self, *_args, **_kwargs):
            def decorator(func):
                return func
            return decorator

    limiter = _NoopLimiter()
# API endpoints
from api.user import user_api 
from api.pfp import pfp_api
from api.nestImg import nestImg_api
from api.post import post_api
from api.channel import channel_api
from api.group import group_api
from api.mod import section_api
from api.nestPost import nestPost_api
from api.messages_api import messages_api
from api.carphoto import car_api
from api.student import student_api
from api.preferences import preferences_api
from api.chat import chat_api
from api.vote import vote_api
from api.sections import sections_bp
from api.route import routes_api 
from api.traffic import traffic_api
from api.savedLocations import savedLocations_api
from api.verify import verify_api
from api.live import incident_api
from api.subscription import subscription_api
from api.stripe_api import stripe_api
from api.businesses import businesses_api

# database Initialization functions
from model.carChat import CarChat
from model.user import User, initUsers
from model.mod import Section, initSections
from model.group import Group, initGroups
from model.channel import Channel, initChannels
from model.post import Post, initPosts
from model.nestPost import NestPost, initNestPosts
from model.vote import Vote, initVotes
from model.savedLocations import SavedLocations, initSavedLocations
from model.subscription import Subscription, SubscriptionRequest, PaymentHistory, RouteUsage, initSubscriptions


# server only View

# register URIs for api endpoints
app.register_blueprint(messages_api)
app.register_blueprint(pfp_api) 
app.register_blueprint(user_api)
app.register_blueprint(channel_api)
app.register_blueprint(group_api)
app.register_blueprint(sections_bp)
app.register_blueprint(nestPost_api)
app.register_blueprint(nestImg_api)
app.register_blueprint(vote_api)
app.register_blueprint(car_api)
app.register_blueprint(student_api)
app.register_blueprint(preferences_api)
app.register_blueprint(post_api, url_prefix='/api')
app.register_blueprint(routes_api, url_prefix='/api')
app.register_blueprint(traffic_api, url_prefix='/api')
app.register_blueprint(chat_api, url_prefix='/api')
app.register_blueprint(savedLocations_api)  # Registering favoriteBooks API
app.register_blueprint(verify_api)
app.register_blueprint(incident_api)
app.register_blueprint(subscription_api)
app.register_blueprint(stripe_api)
app.register_blueprint(businesses_api)


# Tell Flask-Login the view function name of your login route
login_manager.login_view = "login"

@login_manager.unauthorized_handler
def unauthorized_callback():
    return redirect(url_for('login', next=request.path))

# register URIs for server pages
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.context_processor
def inject_user():
    return dict(current_user=current_user)

# Helper function to check if the URL is safe for redirects
def is_safe_url(target):
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute", methods=["POST"])  # throttle brute-force attempts
def login():
    error = None
    next_page = request.args.get('next', '') or request.form.get('next', '')
    if request.method == 'POST':
        username = request.form.get('username', '')
        user = User.query.filter_by(_uid=username).first()
        if user and user.is_password(request.form.get('password', '')):
            login_user(user)
            audit("login_success", username=username)
            if not is_safe_url(next_page):
                return abort(400)
            return redirect(next_page or url_for('index'))
        else:
            audit("login_failure", username=username)
            error = 'Invalid username or password.'
    return render_template("login.html", error=error, next=next_page)
    
@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.errorhandler(404)  # catch for URL not found
def page_not_found(e):
    return render_template('404.html'), 404

@app.route('/')  # connects default URL to index() function
def index():
    print("Home:", current_user)
    return render_template("index.html")

@app.route('/users/table')
@login_required
@admin_required
@ip_allowlist()
def utable():
    users = User.query.all()
    return render_template("utable.html", user_data=users)

@app.route('/users/table2')
@login_required
@admin_required
@ip_allowlist()
def u2table():
    users = User.query.all()
    return render_template("u2table.html", user_data=users)

# Helper function to extract uploads for a user
@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(current_app.config['UPLOAD_FOLDER'], filename)
 
@app.route('/users/delete/<int:user_id>', methods=['DELETE'])
@login_required
@admin_required
@ip_allowlist()
def delete_user(user_id):
    user = User.query.get(user_id)
    if user:
        user.delete()
        audit("user_deleted", target_user_id=user_id)
        return jsonify({'message': 'User deleted successfully'}), 200
    return jsonify({'error': 'User not found'}), 404

@app.route('/users/reset_password/<int:user_id>', methods=['POST'])
@login_required
@admin_required
@ip_allowlist()
def reset_password(user_id):
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    # Set the new password
    if user.update({"password": app.config['DEFAULT_PASSWORD']}):
        audit("password_reset", target_user_id=user_id)
        return jsonify({'message': 'Password reset successfully'}), 200
    audit("password_reset_failed", target_user_id=user_id)
    return jsonify({'error': 'Password reset failed'}), 500

# Create an AppGroup for custom commands
custom_cli = AppGroup('custom', help='Custom commands')

# Define a command to run the data generation functions
@custom_cli.command('generate_data')
def generate_data():
    try:
        initUsers()
    except Exception as e:
        print(f"Error in initUsers: {e}")

    try:
        initSections()
    except Exception as e:
        print(f"Error in initSections: {e}")

    try:
        initGroups()
    except Exception as e:
        print(f"Error in initGroups: {e}")

    try:
        initChannels()
    except Exception as e:
        print(f"Error in initChannels: {e}")

    try:
        initPosts()
    except Exception as e:
        print(f"Error in initPosts: {e}")

    try:
        initNestPosts()
    except Exception as e:
        print(f"Error in initNestPosts: {e}")

    try:
        initVotes()
    except Exception as e:
        print(f"Error in initVotes: {e}")
    
    try:
        initSavedLocations()
    except Exception as e:
        print(f"Error in initSavedLocations: {e}")

    try:
        initSubscriptions()
    except Exception as e:
        print(f"Error in initSubscriptions: {e}")

# Backup the old database
def backup_database(db_uri, backup_uri):
    """Backup the current database."""
    if backup_uri:
        db_path = db_uri.replace('sqlite:///', 'instance/')
        backup_path = backup_uri.replace('sqlite:///', 'instance/')
        shutil.copyfile(db_path, backup_path)
        print(f"Database backed up to {backup_path}")
    else:
        print("Backup not supported for production database.")

# Extract data from the existing database
def extract_data():
    data = {}
    with app.app_context():
        data['users'] = [user.read() for user in User.query.all()]
        data['sections'] = [section.read() for section in Section.query.all()]
        data['groups'] = [group.read() for group in Group.query.all()]
        data['channels'] = [channel.read() for channel in Channel.query.all()]
        data['posts'] = [post.read() for post in Post.query.all()]
        data['locations'] = [post.read() for post in SavedLocations.query.all()]
    return data

# Save extracted data to JSON files
def save_data_to_json(data, directory='backup'):
    if not os.path.exists(directory):
        os.makedirs(directory)
    for table, records in data.items():
        with open(os.path.join(directory, f'{table}.json'), 'w') as f:
            json.dump(records, f)
    print(f"Data backed up to {directory} directory.")

# Load data from JSON files
def load_data_from_json(directory='backup'):
    data = {}
    for table in ['users', 'sections', 'groups', 'channels', 'posts', 'locations']:  # New entry
        try:
            with open(os.path.join(directory, f'{table}.json'), 'r') as f:
                data[table] = json.load(f)
        except FileNotFoundError:
            print(f"Warning: {table}.json not found, skipping...")
    return data

# Restore data to the new database
def restore_data(data):
    with app.app_context():
        users = User.restore(data.get('users', []))
        _ = Section.restore(data.get('sections', []))
        _ = Group.restore(data.get('groups', []), users)
        _ = Channel.restore(data.get('channels', []))
        _ = Post.restore(data.get('posts', []))
        _ = SavedLocations.restore(data.get('locations', []))
    print("Data restored to the new database.")

# Define a command to backup data
@custom_cli.command('backup_data')
def backup_data():
    data = extract_data()
    save_data_to_json(data)
    backup_database(app.config['SQLALCHEMY_DATABASE_URI'], app.config['SQLALCHEMY_BACKUP_URI'])

# Define a command to restore data
@custom_cli.command('restore_data')
def restore_data_command():
    data = load_data_from_json()
    restore_data(data)
    
# Register the custom command group with the Flask application
app.cli.add_command(custom_cli)
        
# this runs the flask application on the development server
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8888)
