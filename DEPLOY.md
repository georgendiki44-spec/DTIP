# DTIP v4 — Deployment Guide

## Prerequisites
- Python 3.11+
- PostgreSQL 15+
- Nginx (for reverse proxy)
- Domain with SSL (Let's Encrypt)
- IntaSend LIVE account

---

## 1. Setup

```bash
# Clone / upload project
cd /var/www/dtip

# Create virtualenv
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

## 2. Environment Variables

```bash
cp .env.example .env
nano .env   # Fill in ALL values — app will refuse to start if any are missing
```

Required variables:
| Variable | Example |
|---|---|
| SECRET_KEY | `openssl rand -hex 32` |
| JWT_SECRET | `openssl rand -hex 32` |
| DATABASE_URL | `postgresql://dtip:pass@localhost/dtip_db` |
| GOOGLE_CLIENT_ID | From Google Cloud Console |
| GOOGLE_CLIENT_SECRET | From Google Cloud Console |
| GOOGLE_REDIRECT_URI | `https://yourdomain.com/auth/google/callback` |
| INTASEND_API_KEY | From IntaSend dashboard (LIVE) |
| INTASEND_SECRET | Webhook signing secret |
| ADMIN_EMAIL | Your admin email |
| ADMIN_PASSWORD | Strong password |
| BASE_URL | `https://yourdomain.com` |

---

## 3. Database

```bash
# Create PostgreSQL DB
sudo -u postgres psql
CREATE DATABASE dtip_db;
CREATE USER dtip WITH PASSWORD 'yourpassword';
GRANT ALL PRIVILEGES ON DATABASE dtip_db TO dtip;
\q

# Run migrations
flask db init
flask db migrate -m "initial"
flask db upgrade
```

---

## 4. Google OAuth Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → Enable "Google+ API" / "Google Identity"
3. OAuth Consent Screen → configure
4. Credentials → Create OAuth 2.0 Client ID
5. Authorised redirect URIs: `https://yourdomain.com/auth/google/callback`
6. Copy Client ID and Secret to `.env`

**Important**: The frontend must call `/api/auth/me` on page load to detect
a Google login session. The token is delivered via HttpOnly cookie — not in
the URL. Update your frontend JS like this:

```javascript
// On page load — check if user is logged in (handles Google OAuth callback)
async function checkSession() {
  try {
    const resp = await fetch('/api/auth/me', { credentials: 'include' });
    if (resp.ok) {
      const data = await resp.json();
      // User is authenticated via cookie
      setUser(data.user);
    }
  } catch (e) { /* not logged in */ }
}
window.addEventListener('load', checkSession);

// After checkSession, also check URL for ?login=google
const params = new URLSearchParams(window.location.search);
if (params.get('login') === 'google') {
  // Remove param from URL without reload
  history.replaceState({}, '', '/');
}
```

---

## 5. IntaSend Webhook

In IntaSend dashboard:
- Settings → Webhooks → Add webhook URL:
  `https://yourdomain.com/webhook/intasend`
- Events: `PAYMENT_COMPLETE`, `PAYMENT_FAILED`, `PAYMENT_CANCELLED`
- Copy the webhook signing secret to `INTASEND_SECRET` in `.env`

---

## 6. Gunicorn (systemd service)

```ini
# /etc/systemd/system/dtip.service
[Unit]
Description=DTIP Flask App
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/var/www/dtip
Environment="PATH=/var/www/dtip/venv/bin"
EnvironmentFile=/var/www/dtip/.env
ExecStart=/var/www/dtip/venv/bin/gunicorn -c gunicorn.conf.py 'app:app'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable dtip
sudo systemctl start dtip
sudo systemctl status dtip
```

---

## 7. Nginx

```nginx
server {
    listen 80;
    server_name yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;

    client_max_body_size 20M;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # WebSocket support for SocketIO
    location /socket.io/ {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 86400;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/dtip /etc/nginx/sites-enabled/
sudo certbot --nginx -d yourdomain.com
sudo nginx -t && sudo systemctl reload nginx
```

---

## 8. File Uploads (Production)

For production, move uploads to S3 or similar:
1. Add `boto3` to requirements.txt
2. In `utils/security.py`, replace `f.save(...)` with S3 upload
3. Update `serve_upload` route to redirect to S3 signed URLs

---

## 9. Health Check

```bash
curl https://yourdomain.com/api/settings/public
```

Expected: `{"activation_fee": "299.0", ...}`
