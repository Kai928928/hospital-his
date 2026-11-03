from datetime import timedelta
from pathlib import Path
import os
import re
import secrets
from urllib.parse import unquote, urlsplit

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from models import Doctor, OperationLog, User, db


auth = Blueprint('auth', __name__)
ROLES = {'admin': '管理员', 'frontdesk': '挂号员', 'doctor': '医生', 'cashier': '收费员'}
HOME = {'admin': 'index', 'frontdesk': 'registration_list', 'doctor': 'medical_record_list', 'cashier': 'bill_list'}
PERMISSIONS = {
    'index': {'admin'},
    'patient_list': set(ROLES), 'patient_detail': set(ROLES),
    'patient_create': {'admin', 'frontdesk'},
    'doctor_list': {'admin', 'frontdesk', 'doctor'}, 'doctor_create': {'admin'},
    'schedule_list': {'admin', 'frontdesk', 'doctor'}, 'schedule_create': {'admin'},
    'schedule_edit': {'admin'}, 'schedule_status': {'admin'},
    'registration_list': {'admin', 'frontdesk', 'doctor'},
    'registration_create': {'admin', 'frontdesk'}, 'registration_status': {'admin', 'frontdesk'},
    'medical_record_list': {'admin', 'doctor'}, 'medical_record_create': {'admin', 'doctor'},
    'medical_record_detail': {'admin', 'doctor'},
    'prescription_list': {'admin', 'doctor'}, 'prescription_create': {'admin', 'doctor'},
    'prescription_detail': {'admin', 'doctor'},
    'medicines.list_': {'admin', 'doctor', 'cashier'}, 'medicines.create': {'admin'},
    'medicines.edit': {'admin'}, 'medicines.status': {'admin'},
    'bill_list': {'admin', 'cashier'}, 'bill_detail': {'admin', 'cashier'},
    'bill_pay': {'admin', 'cashier'}, 'bill_refund': {'admin', 'cashier'},
    'bill_transactions': {'admin', 'cashier'},
    'auth.users': {'admin'}, 'auth.user_create': {'admin'},
    'auth.user_status': {'admin'}, 'auth.user_reset': {'admin'}, 'auth.logs': {'admin'},
    'auth.password': set(ROLES), 'auth.logout': set(ROLES),
}
ACTION_LABELS = {
    'auth.setup': '创建管理员', 'auth.login': '登录', 'auth.logout': '退出登录',
    'auth.password': '修改密码', 'user.create': '新增账号', 'user.status': '账号状态变更',
    'user.reset': '重置密码', 'patient.create': '患者建档', 'doctor.create': '新增医生',
    'registration.create': '门诊挂号', 'registration.status': '挂号状态变更',
    'record.create': '保存病历', 'prescription.create': '开立处方',
    'bill.pay': '确认收款', 'bill.refund': '登记退费',
    'schedule.create': '新增排班', 'schedule.edit': '调整排班', 'schedule.status': '号源开放状态变更',
    'medicine.create': '新增药品', 'medicine.edit': '调整药品资料', 'medicine.status': '药品启停',
}


def can(endpoint):
    return bool(g.get('user') and g.user.role in PERMISSIONS.get(endpoint, set()))


def csrf_token():
    if 'payment_token' not in session:
        session['payment_token'] = secrets.token_hex(32)
    return session['payment_token']


def audit(action, target_type, target_id, summary):
    db.session.add(OperationLog(user_id=g.user.id,
                               actor_name=f'{g.user.display_name} ({g.user.username})',
                               action=action, target_type=target_type, target_id=target_id, summary=summary))


def password_error(password):
    return '密码须为 8 至 128 个字符' if not 8 <= len(password) <= 128 else None


def account_fields():
    username = request.form.get('username', '').strip().lower()
    name = request.form.get('display_name', '').strip()
    password = request.form.get('password', '')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,31}', username):
        raise ValueError('账号须为 3 至 32 位英文字母、数字、点、下划线或短横线，并以字母或数字开头')
    if not 1 <= len(name) <= 64:
        raise ValueError('请填写姓名，最多 64 个字')
    if password_error(password):
        raise ValueError(password_error(password))
    if password != request.form.get('confirm_password', ''):
        raise ValueError('两次输入的密码不一致')
    return username, name, generate_password_hash(password)


def login_target(target):
    decoded = unquote(target)
    try:
        parts = urlsplit(decoded)
    except ValueError:
        return url_for(HOME[g.user.role])
    if (parts.scheme or parts.netloc or not decoded.startswith('/') or decoded.startswith('//')
            or '\\' in decoded or any(ord(char) < 32 for char in decoded)):
        return url_for(HOME[g.user.role])
    return target


def init_auth(app):
    key = os.environ.get('HIS_SECRET_KEY')
    if not key:
        key_path = Path(app.instance_path) / 'session-key'
        key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with key_path.open('x', encoding='ascii') as file:
                file.write(secrets.token_hex(32))
        except FileExistsError:
            pass
        key = key_path.read_text(encoding='ascii').strip()
    app.config.update(SECRET_KEY=key, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=8))
    app.register_blueprint(auth)
    app.context_processor(lambda: {'current_user': g.get('user'), 'can': can,
                                   'csrf_token': csrf_token, 'role_labels': ROLES,
                                   'action_labels': ACTION_LABELS})

    @app.before_request
    def check_access():
        g.user = None
        if request.endpoint is None or request.endpoint == 'static':
            return None
        user_id = session.get('user_id')
        if user_id:
            user = db.session.get(User, user_id)
            if user and user.is_active and user.session_version == session.get('session_version'):
                g.user = user
            else:
                session.clear()
        public = request.endpoint in ('auth.login', 'auth.setup')
        if not public and not g.user:
            if not User.query.first():
                return redirect(url_for('auth.setup'))
            target = request.full_path.rstrip('?') if request.method == 'GET' else '/'
            return redirect(url_for('auth.login', next=target), code=303)
        if request.endpoint == 'index' and g.user and g.user.role != 'admin':
            return redirect(url_for(HOME[g.user.role]))
        if not public and not can(request.endpoint):
            abort(403)
        if request.method == 'POST':
            token = request.form.get('payment_token', '')
            if not token or not secrets.compare_digest(token.encode(), session.get('payment_token', '').encode()):
                return render_template('auth/error.html', message='页面已失效，请刷新后重新提交。'), 403

    @app.errorhandler(403)
    def forbidden(error):
        return render_template('auth/error.html', message='当前账号没有此项操作权限。'), 403

    @app.after_request
    def private_response(response):
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store'
        return response


@auth.route('/setup', methods=['GET', 'POST'])
def setup():
    if User.query.first():
        return redirect(url_for('auth.login'))
    error = None
    if request.method == 'POST':
        try:
            username, name, password_hash = account_fields()
            user = User(id=1, username=username, display_name=name, password_hash=password_hash, role='admin')
            db.session.add(user)
            db.session.flush()
            g.user = user
            audit('auth.setup', 'user', user.id, '首次创建管理员')
            db.session.commit()
            session.clear()
            flash('管理员已创建，请登录。', 'success')
            return redirect(url_for('auth.login'))
        except ValueError as invalid:
            error = str(invalid)
        except IntegrityError:
            db.session.rollback()
            return redirect(url_for('auth.login'))
    return render_template('auth/login.html', setup=True, error=error), 400 if error else 200


@auth.route('/login', methods=['GET', 'POST'])
def login():
    if not User.query.first():
        return redirect(url_for('auth.setup'))
    if g.user:
        return redirect(url_for(HOME[g.user.role]))
    error = None
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username', '').strip().lower()).first()
        if user and user.is_active and check_password_hash(user.password_hash, request.form.get('password', '')):
            session.clear()
            session.permanent = True
            session['user_id'] = user.id
            session['session_version'] = user.session_version
            g.user = user
            audit('auth.login', 'user', user.id, '登录系统')
            db.session.commit()
            return redirect(login_target(request.form.get('next', '/')))
        error = '账号或密码错误，或账号已停用'
    return render_template('auth/login.html', setup=False, error=error), 400 if error else 200


@auth.post('/logout')
def logout():
    audit('auth.logout', 'user', g.user.id, '退出系统')
    db.session.commit()
    session.clear()
    return redirect(url_for('auth.login'))


@auth.route('/account/password', methods=['GET', 'POST'])
def password():
    error = None
    if request.method == 'POST':
        new = request.form.get('password', '')
        if not check_password_hash(g.user.password_hash, request.form.get('old_password', '')):
            error = '原密码不正确'
        elif password_error(new):
            error = password_error(new)
        elif new != request.form.get('confirm_password', ''):
            error = '两次输入的密码不一致'
        if not error:
            g.user.password_hash = generate_password_hash(new)
            g.user.session_version += 1
            audit('auth.password', 'user', g.user.id, '修改本人密码')
            db.session.commit()
            session.clear()
            flash('密码已更新，请重新登录。', 'success')
            return redirect(url_for('auth.login'))
    return render_template('auth/password.html', error=error), 400 if error else 200


@auth.get('/users')
def users():
    return render_template('auth/users.html', users=User.query.order_by(User.id).all())


@auth.route('/users/create', methods=['GET', 'POST'])
def user_create():
    error = None
    if request.method == 'POST':
        try:
            username, name, password_hash = account_fields()
            role = request.form.get('role', '')
            if role not in ROLES:
                raise ValueError('请选择有效岗位')
            doctor_id = request.form.get('doctor_id', type=int) if role == 'doctor' else None
            if role == 'doctor' and (not doctor_id or not db.session.get(Doctor, doctor_id)):
                raise ValueError('医生账号须关联接诊医生')
            user = User(username=username, display_name=name, password_hash=password_hash, role=role, doctor_id=doctor_id)
            db.session.add(user)
            db.session.flush()
            audit('user.create', 'user', user.id, f'新增账号 {username}，岗位：{ROLES[role]}')
            db.session.commit()
            flash('账号已创建。', 'success')
            return redirect(url_for('auth.users'))
        except ValueError as invalid:
            error = str(invalid)
        except IntegrityError:
            db.session.rollback()
            error = '账号已存在，或该医生已关联其他账号'
    return render_template('auth/user_create.html', doctors=Doctor.query.order_by(Doctor.name).all(), error=error), 400 if error else 200


@auth.post('/users/<int:uid>/status')
def user_status(uid):
    user = db.get_or_404(User, uid)
    enabled = request.form.get('enabled')
    if enabled not in ('yes', 'no') or (uid == g.user.id and enabled == 'no'):
        abort(403)
    allowed = [User.id == uid, User.is_active.is_(not (enabled == 'yes'))]
    if user.role == 'admin' and enabled == 'no':
        active_admins = select(func.count(User.id)).where(User.role == 'admin', User.is_active.is_(True)).scalar_subquery()
        allowed.append(active_admins > 1)
    changed = db.session.execute(update(User).where(*allowed).values(
        is_active=(enabled == 'yes'), session_version=User.session_version + 1)
        .execution_options(synchronize_session=False)).rowcount
    if changed:
        audit('user.status', 'user', uid, f'{"启用" if enabled == "yes" else "停用"}账号 {user.username}')
        db.session.commit()
        flash('账号状态已更新。', 'success')
    else:
        db.session.rollback()
        flash('状态已变化，或不能停用最后一位管理员。', 'info')
    return redirect(url_for('auth.users'))


@auth.post('/users/<int:uid>/reset-password')
def user_reset(uid):
    user = db.get_or_404(User, uid)
    new = request.form.get('password', '')
    if uid == g.user.id:
        return redirect(url_for('auth.password'))
    error = password_error(new)
    if new != request.form.get('confirm_password', ''):
        error = '两次输入的密码不一致'
    if error:
        flash(error, 'danger')
        return redirect(url_for('auth.users'))
    user.password_hash = generate_password_hash(new)
    user.session_version += 1
    audit('user.reset', 'user', uid, f'重置账号 {user.username} 的密码')
    db.session.commit()
    flash('密码已重置，该账号的旧登录已失效。', 'success')
    return redirect(url_for('auth.users'))


@auth.get('/operation-logs')
def logs():
    q = request.args.get('q', '').strip()
    action = request.args.get('action', '')
    query = OperationLog.query
    if q:
        query = query.filter(OperationLog.actor_name.contains(q))
    if action:
        query = query.filter(OperationLog.action == action)
    page = query.order_by(OperationLog.id.desc()).paginate(page=request.args.get('page', 1, type=int), per_page=20, error_out=False)
    return render_template('auth/logs.html', pagination=page, q=q, action=action)
