# DTIP v2.0 - Complete Deployment Guide

## 🚀 What You're Getting

A **production-ready** Flask web application with:

✅ **Full Authentication**
- Email/password login & registration
- Google OAuth (ready to connect)
- JWT tokens
- Secure password hashing

✅ **Wallet & Payments**
- M-Pesa deposits via IntaSend STK Push
- Withdrawals to M-Pesa
- Real-time balance tracking
- Complete transaction ledger
- Escrow system for tasks

✅ **Task Marketplace**
- Post tasks with budget & deadline
- Apply to tasks with cover letter
- Client approval workflow
- Auto-payment on completion
- Search & filtering

✅ **Admin Panel**
- Full user management
- Task control (delete, feature, complete)
- Payment approvals
- Dynamic site settings (no restart needed)
- Activity monitoring

✅ **Referral System**
- Unique referral codes
- Signup bonuses
- First task bonuses
- First deposit bonuses

✅ **Production Features**
- Rate limiting on auth
- SQL injection protection
- CSRF protection
- Secure sessions
- Comprehensive logging

---

## ⚡ Deploy in 5 Minutes

### Option 1: Railway (Recommended)

1. **Fork/clone repo to GitHub**
   ```bash
   git clone <your-repo>
   cd dtip
   ```

2. **Sign up at Railway** (railway.app)
   - Click "New Project"
   - Select "Deploy from GitHub"
   - Choose your repo

3. **Add Environment Variables**
   - Go to "Variables" tab
   - Add these (from `.env.example`):
     ```
     PORT=5000
     SECRET_KEY=<random-string>
     ENV=prod
     INTASEND_API_KEY=<your-key>
     INTASEND_PUB_KEY=<your-key>
     ```

4. **Deploy**
   - Railway auto-deploys
   - URL appears in "Domains" tab
   - That's it! ✅

### Option 2: Render

1. **Push to GitHub**
2. **Create new service on Render**
   - Choose "Web Service"
   - Select GitHub repo
   - Build command: `pip install -r requirements.txt`
   - Start command: `python main.py`
3. **Add environment variables**
4. **Deploy** - Done!

### Option 3: Local Testing

```bash
# Install dependencies
pip install -r requirements.txt

# Run server
python main.py

# Visit
open http://localhost:5000
```

Demo credentials:
- Admin: `admin@dtip.co.ke` / `Admin@2024!`
- User: `alice@demo.com` / `Demo@123!`

---

## 🔑 IntaSend M-Pesa Integration

### Get Your Keys
1. Sign up at **intasend.com**
2. Go to Dashboard → API Keys
3. Copy:
   - **API Key** → `INTASEND_API_KEY`
   - **Publishable Key** → `INTASEND_PUB_KEY`

### Configure Webhook
1. In Railway/Render, get your app URL
2. In IntaSend dashboard, set webhook:
   ```
   https://your-app.railway.app/api/payments/callback
   ```
3. Enable "Payment Complete" event

### Test Payment Flow
1. Open app, log in as demo user
2. Go to "Wallet"
3. Click "Deposit via M-Pesa"
4. Enter amount & phone (e.g., +254711111111)
5. STK push should trigger on M-Pesa phone
6. In **DEMO MODE**, auto-completes instantly
7. Check "Transaction History" - should show deposit ✅

---

## 📊 Database

**Uses SQLite by default** (auto-created)

For production, upgrade to PostgreSQL:

### Railway PostgreSQL Add-on

1. In Railway project, click "Add Service"
2. Select "PostgreSQL"
3. Set environment variable:
   ```
   DATABASE_URL=postgresql://user:pass@host:5432/dtip
   ```
4. App detects & uses automatically

### Manual PostgreSQL Setup

```bash
# Create database
createdb dtip_db

# Set in .env
DATABASE_URL=postgresql://user:pass@localhost/dtip
```

---

## 👤 Admin Panel Access

1. Login as: `admin@dtip.co.ke` / `Admin@2024!`
2. Go to Dashboard
3. Click "Admin" (⚡ icon)
4. Manage:
   - **Users**: Ban, edit roles, adjust balances
   - **Tasks**: Delete, feature, force complete
   - **Payments**: View, approve, reject
   - **Settings**: Change fees, enable/disable features

### Key Settings
- `platform_fee` - Task fee %
- `withdrawal_fee` - KES fee per withdrawal
- `gold_price` / `diamond_price` - Membership costs
- `fake_activity_enabled` - Show demo activity
- `maintenance_mode` - Put site offline

---

## 🔐 Security Checklist

Before going live:

- [ ] Change `SECRET_KEY` to random value (32+ chars)
- [ ] Set `ENV=prod` in Railway
- [ ] Use production IntaSend keys (not sandbox)
- [ ] Add your domain to `ALLOWED_ORIGINS` if CORS needed
- [ ] Enable HTTPS (Railway/Render do this by default)
- [ ] Set strong password for admin account
- [ ] Configure email notifications (optional)
- [ ] Regular database backups
- [ ] Monitor logs for errors

---

## 📱 Testing Features

### Without IntaSend (Demo Mode)

All payments auto-complete instantly. Great for testing!

### With IntaSend (Real Payments)

Get API keys, add to environment, restart app.

### Test Users

Create more demo users:
1. Go home page
2. Click "Join Free"
3. Fill form (can use fake email)
4. Login & test

---

## 🐛 Troubleshooting

| Problem | Solution |
|---------|----------|
| Database locked | Restart server |
| Payments stuck pending | Check IntaSend keys & webhook |
| Admin panel 403 error | Verify user role is "admin" |
| Deposits not working | Verify IntaSend API keys correct |
| 500 error on startup | Check all env vars set |
| Slow queries | Monitor with Railway analytics |

---

## 📈 Scale & Optimize

**For 10K+ users:**
1. Switch to PostgreSQL
2. Add Redis for caching
3. Enable connection pooling
4. Use CDN for static files
5. Add search indexing

**Railway** can handle all this - just add services! 🎉

---

## 📚 API Documentation

All endpoints return JSON. Auth via `Authorization: Bearer TOKEN` header.

### Example: Create Account
```bash
curl -X POST http://localhost:5000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "name": "John Doe",
    "email": "john@example.com",
    "phone": "+254700000000",
    "password": "SecurePass123!",
    "role": "worker"
  }'
```

### Example: Deposit
```bash
curl -X POST http://localhost:5000/api/wallet/deposit \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "amount": 1000,
    "phone": "+254700000000"
  }'
```

More endpoints in main.py comments.

---

## 🎯 Next Steps

1. **Deploy** to Railway/Render (5 mins)
2. **Add IntaSend keys** if using M-Pesa
3. **Test** with demo accounts
4. **Customize**:
   - Site name & colors in `site_settings`
   - Hero text in database
   - Admin email (optional)
5. **Go Live** 🚀

---

## 💬 Support & Tips

- Check Railway logs: `railway logs`
- Check Render logs: Dashboard → Logs
- Database: Use SQLite browser for `dtip.db`
- Clean demo data before launch: Delete `dtip.db`
- Backup database regularly

---

## 📄 File Structure

```
dtip/
├── main.py              ← ALL code here (32KB single file)
├── requirements.txt     ← Dependencies
├── .env.example        ← Template for variables
├── railway.toml        ← Railway config
└── README.md          ← Full documentation
```

That's it! Single file = easy to manage & deploy. ✅

---

**Ready? Deploy now!** 🚀
