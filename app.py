"""
DTIP v4 — Digital Tasks & Earning Platform
Production-ready Flask application factory.

Run (dev):  python app.py
Run (prod): gunicorn -w 1 --threads 4 -b 0.0.0.0:5000 'app:create_app()'
            (use 1 worker for SocketIO; scale with multiple processes + Redis adapter)
"""

import os
import logging
from datetime import datetime

from flask import Flask, jsonify, render_template_string
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv

# Load .env before config (so _require() can see the values)
load_dotenv()

from config.settings import get_config
from extensions import db, migrate, cache, socketio, limiter

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(get_config())

    # ── Proxy headers (Nginx / Cloud LB) ─────────────────────────────
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    # ── Logging ───────────────────────────────────────────────────────
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
    )

    # ── Extensions ────────────────────────────────────────────────────
    db.init_app(app)
    migrate.init_app(app, db)
    cache.init_app(app)
    socketio.init_app(app)
    limiter.init_app(app)

    # Ensure upload directory exists
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    # ── Blueprints ────────────────────────────────────────────────────
    _register_blueprints(app)

    # ── Socket handlers ───────────────────────────────────────────────
    import routes.sockets  # noqa: F401 — registers @socketio.on handlers

    # ── Error handlers ────────────────────────────────────────────────
    _register_error_handlers(app)

    # ── DB init ───────────────────────────────────────────────────────
    with app.app_context():
        _init_db(app)

    return app


def _register_blueprints(app: Flask):
    from routes.auth     import auth_bp
    from routes.oauth    import oauth_bp
    from routes.tasks    import tasks_bp
    from routes.payments import payments_bp
    from routes.admin    import admin_bp
    from routes.messages import messages_bp, shares_bp

    app.register_blueprint(auth_bp,      url_prefix='/api/auth')
    app.register_blueprint(oauth_bp)                         # /auth/google/*
    app.register_blueprint(tasks_bp)                         # /api/tasks/*  /uploads/*
    app.register_blueprint(payments_bp)                      # /api/activate, /api/withdraw, /webhook/*
    app.register_blueprint(admin_bp)                         # /api/admin/*  /api/settings/*
    app.register_blueprint(messages_bp)                      # /api/messages/* /api/notifications/*
    app.register_blueprint(shares_bp)                        # /api/shares/*

    # Serve the SPA at root
    @app.route('/')
    def index():
        # The original single-file HTML is preserved in templates/index.html
        # so we don't duplicate it here.
        try:
            with open('templates/index.html', 'r') as f:
                return f.read()
        except FileNotFoundError:
            return '<h1>DTIP API is running</h1><p>Frontend not found.</p>', 200


def _register_error_handlers(app: Flask):
    @app.errorhandler(400)
    def bad_request(e):
        return jsonify(error='Bad request', detail=str(e)), 400

    @app.errorhandler(401)
    def unauthorized(e):
        return jsonify(error='Unauthorized'), 401

    @app.errorhandler(403)
    def forbidden(e):
        return jsonify(error='Forbidden'), 403

    @app.errorhandler(404)
    def not_found(e):
        return jsonify(error='Not found'), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return jsonify(error='Method not allowed'), 405

    @app.errorhandler(413)
    def too_large(e):
        return jsonify(error='File too large (max 16 MB)'), 413

    @app.errorhandler(429)
    def rate_limited(e):
        return jsonify(error='Too many requests — slow down'), 429

    @app.errorhandler(500)
    def internal_error(e):
        logger.exception('Internal server error')
        return jsonify(error='Internal server error'), 500


def _init_db(app: Flask):
    """Create tables and seed defaults on first run."""
    from models import User, Wallet, PlatformSettings
    from utils.helpers import gen_code

    db.create_all()

    # Seed platform settings
    defaults = {
        'activation_fee':     str(app.config['DEFAULT_ACTIVATION_FEE']),
        'referral_bonus':     str(app.config['DEFAULT_REFERRAL_BONUS']),
        'premium_fee':        str(app.config['DEFAULT_PREMIUM_FEE']),
        'withdrawal_fee_pct': str(app.config['DEFAULT_WITHDRAWAL_FEE_PCT']),
        'free_daily_limit':   str(app.config['FREE_DAILY_LIMIT']),
        'premium_daily_limit':str(app.config['PREMIUM_DAILY_LIMIT']),
        'share_price':        '100.0',
        'base_url':           app.config['BASE_URL'],
    }
    for k, v in defaults.items():
        if not PlatformSettings.query.filter_by(key=k).first():
            db.session.add(PlatformSettings(key=k, value=v))

    # Seed admin account
    if not User.query.filter_by(role='admin').first():
        admin = User(
            email=app.config['ADMIN_EMAIL'],
            username='admin',
            role='admin',
            is_verified=True,
            is_activated=True,
            referral_code=gen_code(),
        )
        admin.set_password(app.config['ADMIN_PASSWORD'])
        db.session.add(admin)
        db.session.flush()
        db.session.add(Wallet(user_id=admin.id, balance=0))
        logger.info(f'Admin created: {app.config["ADMIN_EMAIL"]}')

    db.session.commit()
    logger.info('Database initialised')


# ─────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────

app = create_app()

if __name__ == '__main__':
    port  = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_ENV', 'production') == 'development'
    logger.info(f'DTIP v4 starting on :{port} | debug={debug}')
    socketio.run(app, host='0.0.0.0', port=port, debug=debug,
                 allow_unsafe_werkzeug=debug)
