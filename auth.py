from flask import Blueprint, request, jsonify, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from functools import wraps
import psycopg2
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash
import os
import re
from datetime import datetime
from database import get_db_connection

auth_bp = Blueprint('auth', __name__)


def get_novacore_db_connection():
    """Get database connection for NovaCore users (PostgreSQL)"""
    password = os.getenv("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError("POSTGRES_PASSWORD environment variable is not set")
    return psycopg2.connect(
        host="localhost",
        port=5432,
        database="novacore",
        user="postgres",
        password=password,
        cursor_factory=RealDictCursor
    )

# User class for Flask-Login
class User(UserMixin):
    def __init__(self, id, employee_id, email, first_name, last_name, role, active, user_color=None):
        self.id = id
        self.employee_id = employee_id
        self.email = email
        self.first_name = first_name
        self.last_name = last_name
        self.role = role
        self.active = active
        self.user_color = user_color
    
    def is_active(self):
        """Override is_active to check our active column"""
        return self.active == 1

# Admin required decorator
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Admin access required'}), 403
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated_function

def init_login_manager(app):
    """Initialize Flask-Login"""
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    
    @login_manager.user_loader
    def load_user(user_id):
        """Load user from database"""
        conn = get_novacore_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT * FROM users WHERE id = %s', (user_id,))
        user_data = cur.fetchone()
        conn.close()
        
        if user_data:
            return User(
                id=user_data['id'],
                employee_id=user_data['employee_id'],
                email=user_data['username'],  # username column contains the email
                first_name=user_data['first_name'],
                last_name=user_data['last_name'],
                role=user_data['role'],
                active=user_data['active'],
                user_color=user_data.get('user_color')
            )
        return None
    
    return login_manager

@auth_bp.route('/api/auth/login', methods=['POST'])
def login():
    """Login endpoint"""
    data = request.json
    
    if not data or not data.get('email') or not data.get('password'):
        return jsonify({'error': 'Email and password required'}), 400
    
    email = data['email'].lower().strip()
    password = data['password']
    
    conn = get_novacore_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT * FROM users WHERE LOWER(username) = %s', (email,))
    user_data = cur.fetchone()
    conn.close()
    
    if not user_data:
        return jsonify({'error': 'Invalid email or password'}), 401
    
    # Check if account is active
    if not user_data['active']:
        return jsonify({'error': 'Account is disabled. Contact administrator.'}), 401
    
    # Verify password
    try:
        password_hash = user_data['password_hash']
        
        if check_password_hash(password_hash, password):
            # Password is correct - create user object and login
            user = User(
                id=user_data['id'],
                employee_id=user_data['employee_id'],
                email=user_data['username'],  # username column contains the email
                first_name=user_data['first_name'],
                last_name=user_data['last_name'],
                role=user_data['role'],
                active=user_data['active'],
                user_color=user_data.get('user_color')
            )
            
            login_user(user, remember=True)

            return jsonify({
                'success': True,
                'user': {
                    'id': user.id,
                    'employee_id': user.employee_id,
                    'email': user.email,
                    'first_name': user.first_name,
                    'last_name': user.last_name,
                    'role': user.role,
                    'user_color': user.user_color
                }
            }), 200
        else:
            return jsonify({'error': 'Invalid email or password'}), 401
    except Exception as e:
        print(f"Login error: {e}")
        return jsonify({'error': 'Login failed'}), 500

@auth_bp.route('/api/auth/logout', methods=['POST'])
@login_required
def logout():
    """Logout endpoint"""
    logout_user()
    return jsonify({'success': True, 'message': 'Logged out successfully'}), 200

@auth_bp.route('/api/auth/check', methods=['GET'])
def check_auth():
    """Check if user is authenticated"""
    if current_user.is_authenticated:
        return jsonify({
            'authenticated': True,
            'user': {
                'id': current_user.id,
                'employee_id': current_user.employee_id,
                'email': current_user.email,
                'first_name': current_user.first_name,
                'last_name': current_user.last_name,
                'role': current_user.role,
                'user_color': current_user.user_color
            }
        }), 200
    else:
        return jsonify({'authenticated': False}), 200

@auth_bp.route('/api/auth/change-password', methods=['POST'])
@login_required
def change_password():
    """Change user password"""
    data = request.json
    
    if not data or not data.get('current_password') or not data.get('new_password'):
        return jsonify({'error': 'Current and new password required'}), 400
    
    current_password = data['current_password']
    new_password = data['new_password']
    
    # Validate new password strength
    if len(new_password) < 8:
        return jsonify({'error': 'New password must be at least 8 characters'}), 400
    
    # Verify current password
    conn = get_novacore_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT password_hash FROM users WHERE id = %s', (current_user.id,))
    user_data = cur.fetchone()
    
    if not user_data:
        conn.close()
        return jsonify({'error': 'User not found'}), 404
    
    try:
        if not check_password_hash(user_data['password_hash'], current_password):
            conn.close()
            return jsonify({'error': 'Current password is incorrect'}), 401
        
        # Hash new password
        from werkzeug.security import generate_password_hash
        new_password_hash = generate_password_hash(new_password)
        
        # Update password
        cur.execute(
            'UPDATE users SET password_hash = %s WHERE id = %s',
            (new_password_hash, current_user.id)
        )
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Password changed successfully'}), 200
        
    except Exception as e:
        conn.close()
        print(f"Password change error: {e}")
        return jsonify({'error': 'Failed to change password'}), 500
        
@auth_bp.route('/api/auth/update-color', methods=['POST'])
@login_required
def update_color():
    """Update user color preference"""
    data = request.json
    
    if not data or not data.get('color'):
        return jsonify({'error': 'Color is required'}), 400
    
    color = data['color']
    
    # Validate color format (hex color)
    if not re.match(r'^#[0-9A-Fa-f]{6}$', color):
        return jsonify({'error': 'Invalid color format. Use hex format like #FF5733'}), 400
    
    try:
        conn = get_novacore_db_connection()
        cur = conn.cursor()
        cur.execute(
            'UPDATE users SET user_color = %s WHERE id = %s',
            (color, current_user.id)
        )
        conn.commit()
        conn.close()
        
        # Update current user object
        current_user.user_color = color
        
        return jsonify({'success': True, 'message': 'Color updated successfully', 'color': color}), 200
        
    except Exception as e:
        print(f"Color update error: {e}")
        return jsonify({'error': 'Failed to update color'}), 500
        
@auth_bp.route('/api/users/all', methods=['GET'])
@login_required
def get_all_users():
    """Get all users (for displaying in call history, etc.)"""
    try:
        conn = get_novacore_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT id, first_name, last_name, user_color, employee_id, role FROM users WHERE active = 1')
        
        users = []
        for row in cur.fetchall():
            users.append({
                'id': row['id'],
                'first_name': row['first_name'],
                'last_name': row['last_name'],
                'user_color': row.get('user_color'),
                'employee_id': row['employee_id'],
                'role': row['role']
            })
        
        conn.close()
        return jsonify(users), 200
        
    except Exception as e:
        print(f"Error fetching users: {e}")
        return jsonify({'error': 'Failed to fetch users'}), 500
        
@auth_bp.route('/api/users/available', methods=['GET'])
@login_required
def get_available_users():
    """
    Get all users with their online/offline status for call transfers.
    Returns user info + Twilio identity + online status.
    """
    try:
        from app import online_users  # Import the online_users dict from app.py
        
        conn = get_novacore_db_connection()
        cur = conn.cursor()
        cur.execute('''
            SELECT id, first_name, last_name, user_color, employee_id, role, last_activity 
            FROM users 
            WHERE active = 1
        ''')
        
        users = []
        for row in cur.fetchall():
            user_id = row['id']
            
            # Check if user is online (in the online_users dict)
            is_online = user_id in online_users
            
            # Get values with defaults to handle None
            first_name = row['first_name'] or ''
            last_name = row['last_name'] or ''
            
            # Build user object
            user = {
                'id': user_id,
                'first_name': first_name,
                'last_name': last_name,
                'full_name': f"{first_name} {last_name}".strip(),
                'user_color': row.get('user_color'),
                'employee_id': row['employee_id'],
                'role': row['role'],
                'identity': row['employee_id'],  # Twilio identity for transfers
                'is_online': is_online,
                'last_activity': row.get('last_activity')
            }
            
            # Don't include the current logged-in user in the transfer list
            if user_id != current_user.id:
                users.append(user)
        
        conn.close()

        # Sort: online users first, then by name
        # Handle None values in sorting
        users.sort(key=lambda x: (not x.get('is_online', False), x.get('first_name', '')))

        return jsonify(users), 200
        
    except Exception as e:
        print(f"Error fetching available users: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Failed to fetch available users'}), 500
