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

# User class for Flask-Login.
#
# Multi-tenant note: users can come from two sources —
#   - NovaCore  (PC Reps employees; legacy, source='nc')
#   - softphone_users  (HaniTech and future tenants; source='sp')
# The session-stored id is namespaced "{source}:{numeric_id}" so the two
# sources don't collide. tenant_id is derived per source and exposed on
# the User object so query sites can scope by current_user.tenant_id.
class User(UserMixin):
    def __init__(self, id, employee_id, email, first_name, last_name,
                 role, active, user_color=None, tenant_id=None, source="nc"):
        # Composite session id: "nc:5" or "sp:3"
        self.id = f"{source}:{id}"
        self.numeric_id = id
        self.source = source
        self.employee_id = employee_id
        self.email = email
        self.first_name = first_name
        self.last_name = last_name
        self.role = role
        self.active = active
        self.user_color = user_color
        self.tenant_id = tenant_id

    def is_active(self):
        """Override is_active to check our active column"""
        return self.active == 1 if self.source == "nc" else bool(self.active)


def _resolve_pc_reps_tenant_id():
    """Look up the pc_reps tenant id once. Cached at module scope."""
    global _PC_REPS_TENANT_ID
    if _PC_REPS_TENANT_ID is None:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM tenants WHERE slug = 'pc_reps'")
            row = cur.fetchone()
            if not row:
                raise RuntimeError("tenants table missing pc_reps row — migrate_tenants.py not run?")
            _PC_REPS_TENANT_ID = row["id"]
        finally:
            conn.close()
    return _PC_REPS_TENANT_ID


_PC_REPS_TENANT_ID = None

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
        """Load user from the right source based on session-stored prefix.

        Supports three forms:
            "nc:5"   — NovaCore user id 5 (PC Reps employee)
            "sp:3"   — softphone_users id 3 (HaniTech/future-tenant user)
            "5"      — legacy session before this change; treat as nc:5
        """
        if isinstance(user_id, str) and ":" in user_id:
            source, numeric_id_str = user_id.split(":", 1)
        else:
            # Legacy session: bare integer string. Pre-multi-tenant sessions
            # were all NovaCore (PC Reps).
            source, numeric_id_str = "nc", user_id

        try:
            numeric_id = int(numeric_id_str)
        except (TypeError, ValueError):
            return None

        if source == "nc":
            return _load_novacore_user(numeric_id)
        if source == "sp":
            return _load_softphone_user(numeric_id)
        return None

    return login_manager


def _load_novacore_user(numeric_id):
    """Load a PC Reps user from the NovaCore users table."""
    conn = get_novacore_db_connection()
    try:
        cur = conn.cursor()
        cur.execute('SELECT * FROM users WHERE id = %s', (numeric_id,))
        user_data = cur.fetchone()
    finally:
        conn.close()

    if not user_data:
        return None

    return User(
        id=user_data['id'],
        employee_id=user_data['employee_id'],
        email=user_data['username'],  # username column contains the email
        first_name=user_data['first_name'],
        last_name=user_data['last_name'],
        role=user_data['role'],
        active=user_data['active'],
        user_color=user_data.get('user_color'),
        tenant_id=_resolve_pc_reps_tenant_id(),
        source='nc',
    )


def _load_softphone_user(numeric_id):
    """Load a HaniTech (or other native-tenant) user from softphone_users."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, tenant_id, employee_id, email, first_name, last_name, "
            "role, active, user_color FROM softphone_users WHERE id = %s",
            (numeric_id,),
        )
        user_data = cur.fetchone()
    finally:
        conn.close()

    if not user_data:
        return None

    return User(
        id=user_data['id'],
        employee_id=user_data['employee_id'],
        email=user_data['email'],
        first_name=user_data['first_name'],
        last_name=user_data['last_name'],
        role=user_data['role'],
        active=user_data['active'],
        user_color=user_data['user_color'],
        tenant_id=user_data['tenant_id'],
        source='sp',
    )

@auth_bp.route('/api/auth/login', methods=['POST'])
def login():
    """Login endpoint — checks softphone_users first, then NovaCore.

    Order matters because PC Reps NovaCore users vastly outnumber
    softphone_users today, but softphone_users is the small explicit
    set (HaniTech accounts) — checking it first short-circuits new
    tenants and only falls through to NovaCore for the unmatched case.
    """
    data = request.json

    if not data or not data.get('email') or not data.get('password'):
        return jsonify({'error': 'Email and password required'}), 400

    email = data['email'].lower().strip()
    password = data['password']

    # 1. Try softphone_users (HaniTech and any future tenant)
    user = _try_login_softphone_user(email, password)
    if user is False:  # found by email but password wrong
        return jsonify({'error': 'Invalid email or password'}), 401
    if user:
        login_user(user, remember=True)
        return jsonify({
            'success': True,
            'user': _user_to_json(user),
            'tenant': _tenant_to_json(user.tenant_id),
        }), 200

    # 2. Fall through to NovaCore (PC Reps employees)
    user = _try_login_novacore_user(email, password)
    if user is False:
        return jsonify({'error': 'Invalid email or password'}), 401
    if user:
        login_user(user, remember=True)
        return jsonify({
            'success': True,
            'user': _user_to_json(user),
            'tenant': _tenant_to_json(user.tenant_id),
        }), 200

    return jsonify({'error': 'Invalid email or password'}), 401


def _user_to_json(user):
    return {
        'id': user.id,
        'employee_id': user.employee_id,
        'email': user.email,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'role': user.role,
        'user_color': user.user_color,
        'tenant_id': user.tenant_id,
    }


def _tenant_to_json(tenant_id):
    """Tenant metadata block for auth responses (drives frontend branding)."""
    if tenant_id is None:
        return None
    try:
        from tenant_context import tenant_by_id
        t = tenant_by_id(tenant_id)
        return {
            'id': t['id'],
            'slug': t['slug'],
            'name': t['name'],
            'phone_number': t['phone_number'],
            'logo_url': t.get('logo_url'),
            'color': t.get('color'),
            'contact_provider': t['contact_provider'],
        }
    except Exception as e:
        print(f"⚠️ tenant_by_id({tenant_id}) failed in auth response: {e}")
        return None


def _try_login_softphone_user(email, password):
    """Returns User on success, False on bad password, None if not found."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, tenant_id, employee_id, email, password_hash, "
            "first_name, last_name, role, active, user_color "
            "FROM softphone_users WHERE LOWER(email) = %s",
            (email,),
        )
        user_data = cur.fetchone()
    finally:
        conn.close()

    if not user_data:
        return None
    if not user_data['active']:
        return False  # treat disabled as bad creds (don't leak existence)

    try:
        if not check_password_hash(user_data['password_hash'], password):
            return False
    except Exception as e:
        print(f"softphone_users login error: {e}")
        return False

    return User(
        id=user_data['id'],
        employee_id=user_data['employee_id'],
        email=user_data['email'],
        first_name=user_data['first_name'],
        last_name=user_data['last_name'],
        role=user_data['role'],
        active=user_data['active'],
        user_color=user_data['user_color'],
        tenant_id=user_data['tenant_id'],
        source='sp',
    )


def _try_login_novacore_user(email, password):
    """Returns User on success, False on bad password, None if not found."""
    conn = get_novacore_db_connection()
    try:
        cur = conn.cursor()
        cur.execute('SELECT * FROM users WHERE LOWER(username) = %s', (email,))
        user_data = cur.fetchone()
    finally:
        conn.close()

    if not user_data:
        return None
    if not user_data['active']:
        return False

    try:
        if not check_password_hash(user_data['password_hash'], password):
            return False
    except Exception as e:
        print(f"NovaCore login error: {e}")
        return False

    return User(
        id=user_data['id'],
        employee_id=user_data['employee_id'],
        email=user_data['username'],
        first_name=user_data['first_name'],
        last_name=user_data['last_name'],
        role=user_data['role'],
        active=user_data['active'],
        user_color=user_data.get('user_color'),
        tenant_id=_resolve_pc_reps_tenant_id(),
        source='nc',
    )

@auth_bp.route('/api/auth/logout', methods=['POST'])
@login_required
def logout():
    """Logout endpoint"""
    logout_user()
    return jsonify({'success': True, 'message': 'Logged out successfully'}), 200

@auth_bp.route('/api/auth/check', methods=['GET'])
def check_auth():
    """Check if user is authenticated. Returns tenant block too so the
    frontend can render branding (logo, name, color) on first paint."""
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
                'user_color': current_user.user_color,
                'tenant_id': current_user.tenant_id,
            },
            'tenant': _tenant_to_json(current_user.tenant_id),
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
    
    # Source-aware password check + update — NovaCore for PC Reps,
    # softphone_users for HaniTech (and any future tenant).
    if current_user.source == 'sp':
        conn = get_db_connection()
        select_sql = "SELECT password_hash FROM softphone_users WHERE id = %s"
        update_sql = "UPDATE softphone_users SET password_hash = %s WHERE id = %s"
    else:
        conn = get_novacore_db_connection()
        select_sql = "SELECT password_hash FROM users WHERE id = %s"
        update_sql = "UPDATE users SET password_hash = %s WHERE id = %s"

    cur = conn.cursor()
    cur.execute(select_sql, (current_user.numeric_id,))
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

        cur.execute(update_sql, (new_password_hash, current_user.numeric_id))
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
        # Source-aware color update
        if current_user.source == 'sp':
            conn = get_db_connection()
            update_sql = "UPDATE softphone_users SET user_color = %s WHERE id = %s"
        else:
            conn = get_novacore_db_connection()
            update_sql = "UPDATE users SET user_color = %s WHERE id = %s"

        cur = conn.cursor()
        cur.execute(update_sql, (color, current_user.numeric_id))
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
    """Get all users in the current tenant (for displaying in call history, etc.)

    PC Reps users come from NovaCore; HaniTech (and any other native-tenant
    user) comes from softphone_users scoped to that tenant.
    """
    try:
        if current_user.source == 'sp':
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT id, first_name, last_name, user_color, employee_id, role "
                "FROM softphone_users WHERE active = TRUE AND tenant_id = %s",
                (current_user.tenant_id,),
            )
        else:
            conn = get_novacore_db_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT id, first_name, last_name, user_color, employee_id, role "
                "FROM users WHERE active = 1"
            )

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
    Get all users with their online/offline status for call transfers,
    scoped to the current tenant.
    Returns user info + Twilio identity + online status.
    """
    try:
        from app import online_users  # Import the online_users dict from app.py

        if current_user.source == 'sp':
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT id, first_name, last_name, user_color, employee_id, role, "
                "NULL::timestamp as last_activity "
                "FROM softphone_users WHERE active = TRUE AND tenant_id = %s",
                (current_user.tenant_id,),
            )
        else:
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
            
            # Don't include the current logged-in user in the transfer list.
            # current_user.numeric_id matches the integer source id; user_id
            # here is the integer NovaCore id.
            if user_id != current_user.numeric_id:
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
