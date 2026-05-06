"""
DTIP — SocketIO Event Handlers
Auth: JWT passed in socket handshake headers (NOT in URL query param).
"""
import logging

from flask_socketio import join_room, disconnect

from extensions import socketio
from utils.security import decode_token

logger = logging.getLogger(__name__)


def _auth_user_from_socket():
    """
    Extract and validate JWT from socket handshake.
    Accepts:
      - Authorization header: 'Bearer <token>'
      - Cookie: dtip_auth=<token>
    Returns User or None.
    """
    from flask import request as req
    from models import User

    # Try Authorization header first
    auth = req.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '').strip()

    # Fall back to cookie (set during Google OAuth)
    if not token:
        token = req.cookies.get('dtip_auth', '')

    if not token:
        return None

    try:
        data = decode_token(token)
        user = User.query.get(data['sub'])
        return user if user and user.is_active and not user.is_suspended else None
    except Exception:
        return None


@socketio.on('connect')
def on_connect():
    user = _auth_user_from_socket()
    if not user:
        logger.debug('SocketIO: unauthenticated connection rejected')
        disconnect()
        return False   # reject

    join_room(f'user_{user.id}')
    if user.role in ('admin', 'moderator'):
        join_room('moderators')
    logger.debug(f'SocketIO: user {user.id} connected')


@socketio.on('disconnect')
def on_disconnect():
    logger.debug('SocketIO: client disconnected')


@socketio.on('join')
def on_join(data):
    """Allow users to join specific rooms they're authorised for."""
    user = _auth_user_from_socket()
    if not user:
        disconnect()
        return

    room = data.get('room', '')
    # Only admins/mods can join the moderators room
    if room == 'moderators' and user.role not in ('admin', 'moderator'):
        return
    # Users can only join their own room
    if room == f'user_{user.id}' or user.role in ('admin', 'moderator'):
        join_room(room)
