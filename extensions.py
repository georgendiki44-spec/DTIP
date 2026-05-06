"""
DTIP — Shared Flask Extension Instances
Import from here everywhere to avoid circular imports.
"""
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_caching import Cache
from flask_migrate import Migrate

db      = SQLAlchemy()
migrate = Migrate()
cache   = Cache()
socketio = SocketIO(cors_allowed_origins="*", async_mode='threading',
                    logger=False, engineio_logger=False)
limiter  = Limiter(key_func=get_remote_address,
                   default_limits=["500 per day", "100 per hour"],
                   storage_uri="memory://")
