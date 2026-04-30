# 🚀 DTIP — Digital Tasks & Investing Platform

A full-stack web platform for task-based earning, built as a **single Python file** deployable to Railway, Render, or any Python host.

---

## ⚡ Quick Start (Local)

```bash
# 1. Install dependencies
pip install flask gunicorn

# 2. Run
python app.py

# 3. Open http://localhost:8000
# Admin: admin@dtip.co.ke / Admin1234!
```

---

## 🚂 Deploy to Railway (Recommended — Free tier available)

1. Push this folder to a GitHub repo
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Select your repo
4. Add environment variables (see `.env.example`):
   - `SECRET_KEY` — random 64-char string
   - `ADMIN_EMAIL` — your admin email
   - `ADMIN_PASS` — strong password
5. Railway auto-detects `railway.toml` and deploys ✅

---

## 🎨 Deploy to Render (Free tier available)

1. Push to GitHub
2. Go to [render.com](https://render.com) → New → Web Service
3. Connect your repo
4. Set:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2`
5. Add env vars from `.env.example`
6. Deploy ✅

---

## 🔐 Default Admin Login

| Field | Value |
|-------|-------|
| Email | `admin@dtip.co.ke` |
| Password | `Admin1234!` |

> ⚠️ Change these via environment variables before going live!

---

## 👤 User Roles

| Role | Can Do |
|------|--------|
| **Worker** | Browse tasks, apply, submit work, earn |
| **Client** | Post tasks, review submissions, release payments |
| **Admin** | Everything — edit users, tasks, settings, feed |

---

## 💳 M-Pesa Integration (IntaSend)

Currently runs in **demo mode** (deposits are simulated).

To enable real M-Pesa:
1. Sign up at [intasend.com](https://intasend.com)
2. Get your API key and secret
3. Set `INTASEND_API_KEY` and `INTASEND_SECRET` env vars
4. In `app.py`, find the `deposit` action in `/wallet` and replace the demo block with:

```python
import requests
response = requests.post("https://sandbox.intasend.com/api/v1/payment/mpesa-stk-push/", 
    headers={"Authorization": f"Bearer {INTASEND_API_KEY}"},
    json={"amount": amount, "phone_number": phone, "currency": "KES",
          "api_ref": pay_id, "narrative": "DTIP Wallet Deposit"})
```

Webhook endpoint is already live at `/payments/callback`.

---

## 🛡️ Admin Panel Features

- **Users** — Edit name, email, role, membership, ban/unban, credit/debit wallet
- **Tasks** — View and edit all tasks, change status
- **Payments** — Full payment history
- **Withdrawals** — Approve or reject with notes (auto-refund on reject)
- **Settings** — Live-edit platform fee, membership prices, referral bonus, site name, toggle features
- **Activity Feed** — Post, delete, or generate bulk fake activity
- **Fake Activity Generator** — Auto-generates realistic activity every 25–90 seconds

---

## 📁 File Structure

```
dtip/
├── app.py              ← Entire app (Flask + DB + Routes + UI)
├── requirements.txt    ← Flask + Gunicorn
├── Procfile            ← Heroku/Railway process
├── railway.toml        ← Railway config
├── render.yaml         ← Render config
├── .env.example        ← Environment variables template
├── .gitignore
└── README.md
```

---

## 🔧 Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | auto | Flask session secret |
| `DATABASE_URL` | `dtip.db` | SQLite path |
| `ADMIN_EMAIL` | `admin@dtip.co.ke` | Admin login |
| `ADMIN_PASS` | `Admin1234!` | Admin password |
| `PLATFORM_FEE_PCT` | `8` | Task success fee % |
| `MAX_ACTIVE_TASKS` | `5` | Max tasks per worker |
| `INTASEND_API_KEY` | `DEMO_KEY` | IntaSend key |
| `INTASEND_SECRET` | `DEMO_SECRET` | IntaSend secret |
| `PORT` | `8000` | Server port |
| `FLASK_DEBUG` | `0` | Debug mode |

---

## ⚠️ Compliance Notes

- No guaranteed returns — membership benefits are service-based only
- All investment-like features are clearly labeled "simulation"
- Escrow system protects client funds until work approved
- Immutable ledger tracks all financial operations
- Platform fee disclosed transparently

---

## 🔒 Security Checklist (Before Go-Live)

- [ ] Change `ADMIN_PASS` to a strong password
- [ ] Set a random 64-char `SECRET_KEY`
- [ ] Enable HTTPS (automatic on Railway/Render)
- [ ] Set `FLASK_DEBUG=0`
- [ ] Wire real IntaSend API keys
- [ ] Back up the database regularly

---

Built with ❤️ using Flask + SQLite. No external databases needed.
