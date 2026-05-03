# DTIP v2 — Digital Tasks & Earning Platform 🇰🇪

Kenya-focused task marketplace. Single Python file. Production-ready.

## Quick Start (Local)

```bash
pip install -r requirements.txt
cp .env.example .env   # edit your values
python app.py
# → http://localhost:5000
# Admin: admin@dtip.co.ke / Admin@DTIP2024!
```

## Deploy to Railway

1. Push to GitHub
2. Go to railway.app → New Project → Deploy from GitHub
3. Add env vars from `.env.example`
4. Add PostgreSQL plugin → copy `DATABASE_URL` to env
5. Done ✅

## Deploy to Render

1. Push to GitHub
2. render.com → New → Blueprint → connect repo
3. `render.yaml` auto-configures everything
4. Add Google OAuth + IntaSend keys in dashboard

## Deploy to Heroku

```bash
heroku create dtip-v2
heroku addons:create heroku-postgresql:mini
heroku config:set SECRET_KEY=$(openssl rand -hex 32)
heroku config:set JWT_SECRET=$(openssl rand -hex 32)
heroku config:set DEMO_MODE=true
heroku config:set ADMIN_EMAIL=admin@dtip.co.ke
heroku config:set ADMIN_PASSWORD=Admin@DTIP2024!
git push heroku main
```

## Google OAuth Setup

1. console.cloud.google.com → New Project
2. APIs & Services → Credentials → OAuth 2.0 Client ID
3. Authorized redirect URIs: `https://yourdomain.com/auth/google/callback`
4. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`

## Environment Variables

| Variable | Description |
|---|---|
| `SECRET_KEY` | Flask session secret |
| `JWT_SECRET` | JWT signing secret |
| `DATABASE_URL` | PostgreSQL connection string |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth secret |
| `GOOGLE_REDIRECT_URI` | OAuth callback URL |
| `INTASEND_API_KEY` | IntaSend API key |
| `DEMO_MODE` | `true` = instant demo payments |
| `ADMIN_EMAIL` | Initial admin email |
| `ADMIN_PASSWORD` | Initial admin password |
| `REFERRAL_BONUS` | KES bonus per referral (default: 50) |
| `WITHDRAWAL_FEE_PCT` | Withdrawal fee % (default: 5) |

## Features

- ✅ JWT Auth + Google OAuth
- ✅ Admin / Client / Worker roles
- ✅ Task moderation (approve/reject/flag)
- ✅ Auto-moderation (keyword + budget checks)
- ✅ Real-time chat (SocketIO)
- ✅ Admin broadcast to all users
- ✅ Verified client badges
- ✅ M-Pesa via IntaSend (demo mode)
- ✅ Wallet + ledger system
- ✅ Referral system with bonuses
- ✅ Rate limiting
- ✅ SQLite → PostgreSQL ready
- ✅ Railway / Render / Heroku deploy configs
