"""
DTIP — Task Routes
"""
import os
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, current_app, send_from_directory

from extensions import db, limiter, socketio
from models import Task, TaskCompletion, User
from utils.security import (require_auth, require_admin, require_moderator,
                             validate_pdf, safe_filename, hash_file, sanitize)
from utils.helpers import ledger, notify, audit, get_setting

logger   = logging.getLogger(__name__)
tasks_bp = Blueprint('tasks', __name__)

SUSPICIOUS_KEYWORDS = [
    'porn', 'xxx', 'drugs', 'weapon', 'hack',
    'phishing', 'scam', 'casino', 'bet', 'gambling',
]


# ─────────────────────────────────────────
# SERVE UPLOADS (authenticated)
# ─────────────────────────────────────────

@tasks_bp.route('/uploads/<path:filename>')
@require_auth
def serve_upload(user, filename):
    # Only mods/admins can view any file; regular users only their own
    if user.role not in ('admin', 'moderator'):
        comp = TaskCompletion.query.filter_by(
            user_id=user.id, pdf_filename=filename).first()
        if not comp:
            return jsonify(error='Forbidden'), 403
    # Prevent path traversal — send_from_directory is safe but be explicit
    safe = os.path.basename(filename)
    return send_from_directory(current_app.config['UPLOAD_FOLDER'], safe)


# ─────────────────────────────────────────
# LIST / GET TASKS
# ─────────────────────────────────────────

@tasks_bp.route('/api/tasks', methods=['GET'])
def list_tasks():
    page = request.args.get('page', 1, int)
    cat  = request.args.get('category', '')
    q    = request.args.get('q', '')
    qry  = Task.query.filter_by(is_active=True)
    if cat:
        qry = qry.filter_by(category=cat)
    if q:
        qry = qry.filter(
            Task.title.ilike(f'%{q}%') | Task.description.ilike(f'%{q}%')
        )
    tasks = qry.order_by(Task.created_at.desc()).paginate(page=page, per_page=20)
    return jsonify(tasks=[t.to_dict() for t in tasks.items],
                   total=tasks.total, pages=tasks.pages, page=page)


@tasks_bp.route('/api/tasks/<int:tid>', methods=['GET'])
def get_task(tid):
    task = Task.query.get_or_404(tid)
    d    = task.to_dict()
    # Attach current user's completion if authenticated
    from utils.security import get_current_user
    user = get_current_user()
    if user:
        c = TaskCompletion.query.filter_by(task_id=tid, user_id=user.id).first()
        d['my_completion'] = c.to_dict() if c else None
    return jsonify(task=d)


# ─────────────────────────────────────────
# CREATE TASK (admin)
# ─────────────────────────────────────────

@tasks_bp.route('/api/tasks', methods=['POST'])
@require_admin
def create_task(admin):
    d = request.get_json() or {}
    title       = sanitize(d.get('title', ''), 200)
    description = sanitize(d.get('description', ''), 5000)
    instructions= sanitize(d.get('instructions', ''), 10000)
    category    = sanitize(d.get('category', ''), 80)
    reward      = d.get('reward')

    if not title or not description or not category or not reward:
        return jsonify(error='title, description, category, reward required'), 400
    try:
        reward = float(reward)
        if reward <= 0:
            raise ValueError
    except ValueError:
        return jsonify(error='reward must be a positive number'), 400

    flagged = any(kw in (title + ' ' + description).lower()
                  for kw in SUSPICIOUS_KEYWORDS)

    deadline = None
    if d.get('deadline'):
        try:
            deadline = datetime.fromisoformat(d['deadline'])
        except ValueError:
            pass

    task = Task(
        title=title, description=description,
        instructions=instructions, category=category,
        reward=reward, requires_pdf=bool(d.get('requires_pdf', True)),
        deadline=deadline, created_by=admin.id,
        is_flagged=flagged,
        flag_reason='Auto-flagged: suspicious keywords' if flagged else None,
    )
    db.session.add(task)
    db.session.commit()
    audit(admin.id, 'task_created', f'task:{task.id}', title)

    # Notify active users
    try:
        uids = [u.id for u in
                User.query.filter_by(is_active=True, is_activated=True).all()]
        for uid in uids:
            notify(uid, f'🆕 New Task: {title}',
                   f'Earn KES {reward:.0f} — {category}', 'info')
        socketio.emit('new_task', task.to_dict(), to=None)
    except Exception:
        pass

    return jsonify(task=task.to_dict()), 201


# ─────────────────────────────────────────
# UPDATE / DELETE TASK (admin)
# ─────────────────────────────────────────

@tasks_bp.route('/api/tasks/<int:tid>', methods=['PUT'])
@require_admin
def update_task(admin, tid):
    task = Task.query.get_or_404(tid)
    d    = request.get_json() or {}
    for field in ('title', 'description', 'instructions', 'category',
                  'reward', 'is_active', 'requires_pdf'):
        if field in d:
            setattr(task, field,
                    sanitize(str(d[field]), 5000) if isinstance(d[field], str) else d[field])
    db.session.commit()
    audit(admin.id, 'task_updated', f'task:{tid}')
    return jsonify(task=task.to_dict())


@tasks_bp.route('/api/tasks/<int:tid>', methods=['DELETE'])
@require_admin
def delete_task(admin, tid):
    task = Task.query.get_or_404(tid)
    task.is_active = False
    db.session.commit()
    audit(admin.id, 'task_deleted', f'task:{tid}')
    return jsonify(ok=True)


# ─────────────────────────────────────────
# SUBMIT TASK
# ─────────────────────────────────────────

@tasks_bp.route('/api/tasks/<int:tid>/submit', methods=['POST'])
@require_auth
@limiter.limit("20 per hour")
def submit_task(user, tid):
    if not user.is_activated:
        return jsonify(error='Activate your account to submit tasks'), 403

    task = Task.query.get_or_404(tid)
    if not task.is_active:
        return jsonify(error='This task is no longer available'), 400
    if task.deadline and task.deadline < datetime.utcnow():
        return jsonify(error='Deadline passed'), 400

    done_today = user.get_daily_tasks_done()
    limit      = user.daily_limit()
    if done_today >= limit:
        return jsonify(
            error='daily_limit',
            message=f'Daily limit reached ({limit}/day). Upgrade to premium for more.',
            limit=limit, done=done_today,
        ), 429

    if TaskCompletion.query.filter_by(task_id=tid, user_id=user.id).first():
        return jsonify(error='You already submitted this task'), 409

    # ── PDF upload ────────────────────────────────────────────────────
    pdf_filename = None
    pdf_original = None
    pdf_hash     = None

    if 'pdf' in request.files:
        f = request.files['pdf']
        err = validate_pdf(f)
        if err:
            return jsonify(error=err), 400

        # Duplicate file detection
        file_hash = hash_file(f)
        dup = TaskCompletion.query.filter_by(
            task_id=tid, pdf_hash=file_hash).first()
        if dup:
            return jsonify(error='Duplicate file — this PDF was already submitted for this task'), 409

        pdf_original = f.filename
        pdf_filename = safe_filename(f.filename)
        pdf_hash     = file_hash

        upload_path = os.path.join(current_app.config['UPLOAD_FOLDER'], pdf_filename)
        f.save(upload_path)

    if task.requires_pdf and not pdf_filename:
        return jsonify(error='This task requires a PDF submission'), 400

    proof_text = sanitize(request.form.get('proof_text', ''), 10000)

    comp = TaskCompletion(
        task_id=tid, user_id=user.id,
        proof_text=proof_text,
        pdf_filename=pdf_filename,
        pdf_original=pdf_original,
        pdf_hash=pdf_hash,
        status='pending',
    )
    db.session.add(comp)
    user.increment_task_count()
    db.session.commit()

    # Notify reviewers
    mods = User.query.filter(
        User.role.in_(['admin', 'moderator']),
        User.is_active.is_(True),
    ).all()
    for mod in mods:
        notify(mod.id, f'📥 New Submission: {task.title}',
               f'{user.username} submitted task for review', 'info')
    notify(user.id, '📤 Submission Received',
           f'Your submission for "{task.title}" is pending review.', 'info')

    return jsonify(completion=comp.to_dict(), message='Submitted for review'), 201


# ─────────────────────────────────────────
# REVIEW COMPLETIONS (mod/admin)
# ─────────────────────────────────────────

@tasks_bp.route('/api/completions/pending')
@require_moderator
def pending_completions(mod):
    page  = request.args.get('page', 1, int)
    comps = (TaskCompletion.query
             .filter_by(status='pending')
             .order_by(TaskCompletion.created_at.desc())
             .paginate(page=page, per_page=50))
    return jsonify(completions=[c.to_dict() for c in comps.items],
                   total=comps.total, pages=comps.pages)


@tasks_bp.route('/api/completions/<int:cid>/review', methods=['POST'])
@require_moderator
def review_completion(mod, cid):
    comp   = TaskCompletion.query.get_or_404(cid)
    d      = request.get_json() or {}
    action = d.get('action')
    reason = sanitize(d.get('reason', ''), 500)

    if action not in ('approve', 'reject'):
        return jsonify(error='action must be "approve" or "reject"'), 400
    if comp.status != 'pending':
        return jsonify(error='Completion already reviewed'), 409

    comp.status          = 'approved' if action == 'approve' else 'rejected'
    comp.rejection_reason= reason if action == 'reject' else None
    comp.reviewed_by     = mod.id
    comp.reviewed_at     = datetime.utcnow()

    if action == 'approve':
        task = Task.query.get(comp.task_id)
        user = User.query.get(comp.user_id)
        if task and user and user.wallet:
            w = user.wallet
            w.balance      = float(w.balance) + float(task.reward)
            w.total_earned = float(w.total_earned) + float(task.reward)
            ledger(w, 'task_reward', float(task.reward),
                   f'Task reward: {task.title}', str(task.id))
            notify(user.id, '✅ Task Approved & Paid!',
                   f'KES {float(task.reward):.0f} added for "{task.title}"', 'success')
        audit(mod.id, 'completion_approved', f'completion:{cid}',
              f'task:{comp.task_id} user:{comp.user_id}')
    else:
        user = User.query.get(comp.user_id)
        task = Task.query.get(comp.task_id)
        if user:
            notify(user.id, '❌ Submission Rejected',
                   f'Submission for "{task.title if task else "task"}" rejected. {reason}',
                   'error')
        audit(mod.id, 'completion_rejected', f'completion:{cid}', reason)

    db.session.commit()
    return jsonify(completion=comp.to_dict())


# ─────────────────────────────────────────
# MY COMPLETIONS
# ─────────────────────────────────────────

@tasks_bp.route('/api/my/completions')
@require_auth
def my_completions(user):
    page  = request.args.get('page', 1, int)
    comps = (TaskCompletion.query
             .filter_by(user_id=user.id)
             .order_by(TaskCompletion.created_at.desc())
             .paginate(page=page, per_page=20))
    return jsonify(completions=[c.to_dict() for c in comps.items],
                   total=comps.total, pages=comps.pages)
