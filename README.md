# DTIP v3.0 - Enterprise SaaS Platform

Production-ready digital tasks & earning platform with M-Pesa payments, admin control, referrals.

## 📦 What You Get

**Complete Application Features:**
- ✅ User authentication (email/password + JWT)
- ✅ M-Pesa deposits/withdrawals via IntaSend  
- ✅ Task marketplace (post, apply, approve, pay)
- ✅ Admin panel (user management, payments, settings)
- ✅ Referral system (codes, bonuses, tracking)
- ✅ Wallet system (balance, ledger, escrow)
- ✅ Notifications (in-app alerts)
- ✅ Rate limiting & security
- ✅ SQLite database (auto-setup)
- ✅ Responsive UI

## 🚀 Quick Start

### Local Development
```bash
pip install -r requirements.txt
cp .env.example .env
python app.py
```
Visit: http://localhost:5000

Demo: alice@demo.com / Demo@123!
Admin: admin@dtip.co.ke / Admin@2024!

### Deploy to Render

1. Push to GitHub
2. Create Render Web Service
3. Connect GitHub repo
4. Set environment variables
5. Deploy ✅

Environment variables needed:
- SECRET_KEY (random string)
- INTASEND_API_KEY (optional)
- INTASEND_PUB_KEY (optional)

## 📋 Features

### Authentication
- Email/password registration & login
- JWT tokens (24-hour expiry)
- Secure password hashing (Werkzeug)
- Demo accounts pre-loaded

### Wallet System
- Real-time balance tracking
- Transaction ledger
- M-Pesa deposits (STK Push)
- M-Pesa withdrawals  
- Escrow holds for tasks
- Balance validation

### Task Marketplace
- Post tasks (clients only)
- Browse & search tasks
- Apply with cover letter
- Accept applications
- Submit work
- Client approval & payment
- Automatic worker payment
- Categorized tasks

### Admin Panel
- User management (edit, ban, adjust balance)
- Task control (delete, feature, force complete)
- Payment management (approve, reject)
- Dynamic settings (no restart needed)
- Dashboard with metrics

### Referral System
- Unique codes per user
- Bonus on signup
- Bonus on first task
- Bonus on first deposit
- Track referral earnings

### Security
- Rate limiting (auth endpoints)
- SQL injection prevention (parameterized queries)
- Secure password hashing
- JWT token validation
- CSRF protection (Flask default)

## 🛠 API Endpoints

### Auth
- POST /api/auth/register
- POST /api/auth/login
- GET /api/auth/me

### Wallet
- GET /api/wallet
- POST /api/wallet/deposit
- POST /api/wallet/withdraw
- POST /api/payments/callback (webhook)

### Tasks
- GET /api/tasks
- POST /api/tasks
- GET /api/tasks/<id>
- POST /api/tasks/<id>/apply
- POST /api/tasks/<id>/applications/<id>/accept
- POST /api/tasks/<id>/submit
- POST /api/tasks/<id>/approve

### Admin
- GET /api/admin/dashboard
- GET /api/admin/users
- PUT /api/admin/users/<id>
- GET /api/admin/tasks
- DELETE /api/admin/tasks/<id>
- GET /api/admin/payments
- POST /api/admin/settings

## 💳 IntaSend Integration

### Demo Mode (Default)
- Deposits auto-approve instantly
- Perfect for testing
- No API keys needed

### Production Mode
1. Get IntaSend API keys
2. Set environment variables
3. Restart app
4. Real M-Pesa STK Push enabled

### Webhook Setup
- URL: https://your-app.render.app/api/payments/callback
- Enable: Payment Complete event

## 📊 Database

Auto-created tables:
- users
- wallets
- ledger
- tasks
- applications
- reviews
- payments
- notifications
- activity
- site_settings

## 🔧 Configuration

Edit site_settings via admin panel:
- Platform fee percentage
- Withdrawal fee
- Welcome bonus
- Referral bonus
- Gold/Diamond membership prices
- Maintenance mode

Changes apply instantly.

## 🎯 Demo Accounts

**Admin** (Full Control)
- Email: admin@dtip.co.ke
- Password: Admin@2024!
- Balance: KES 50,000

**Worker** (Earn Money)
- Email: alice@demo.com
- Password: Demo@123!
- Balance: KES 5,000

**Client** (Post Tasks)
- Email: bob@demo.com
- Password: Demo@123!

## 📱 UI/UX

- Modern dark theme
- Responsive design
- Modal dialogs
- Toast notifications
- Real-time wallet updates
- Admin sidebar navigation

## 🔒 Security Checklist

- [ ] Change SECRET_KEY (production)
- [ ] Set ENV=production
- [ ] Use production IntaSend keys
- [ ] Enable HTTPS
- [ ] Configure database backups
- [ ] Monitor logs

## 📈 Performance

- Indexed database queries
- Pagination ready
- Optimized API responses
- Rate limiting enabled
- Async webhook handling

## 🚀 Scaling

For production:
1. Switch to PostgreSQL
2. Add Redis caching
3. Use Gunicorn with workers
4. Enable database connection pooling
5. Set up monitoring

## 🐛 Troubleshooting

**"Database locked"**
- Close other connections
- Restart server

**"Payments not working"**
- Verify API keys
- Check webhook configuration
- Review IntaSend logs

**"Admin panel 403"**
- Ensure user role is 'admin'
- Check JWT token

**Deployment issues**
- Check Render logs
- Verify environment variables
- Test locally first

## 📞 Support

Issues? Check:
1. README.md (this file)
2. app.py comments
3. Render logs
4. IntaSend dashboard

## 📄 License

Use, modify, deploy freely. Your code, your terms.

---

**Ready to go live?**

1. Test locally (2 mins)
2. Push to GitHub
3. Deploy to Render (5 mins)
4. Add IntaSend keys (optional)
5. You're live! 🎉
