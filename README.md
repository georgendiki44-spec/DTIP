# DTIP v2.0 - Digital Tasks & Investing Platform

Production-ready Flask application with complete refactor: Google OAuth, IntaSend M-Pesa payments, admin panel, referral system, and full wallet management.

## Quick Start

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Copy environment file
cp .env.example .env

# Run server
python main.py
```

Visit `http://localhost:5000`

**Demo Credentials:**
- Admin: `admin@dtip.co.ke` / `Admin@2024!`
- User: `alice@demo.com` / `Demo@123!`

### Deploy on Railway

1. Push code to GitHub
2. Create Railway project
3. Connect GitHub repo
4. Add environment variables from `.env.example`
5. Railway auto-deploys on `git push`

**Set these in Railway:**
```
PORT=5000
SECRET_KEY=<generate-random-key>
ENV=prod
INTASEND_API_KEY=<your-key>
INTASEND_PUB_KEY=<your-key>
```

## Features

✅ **Authentication**
- Email/password registration & login
- Google OAuth integration  
- JWT tokens with refresh
- Secure password hashing

✅ **Wallet System**
- Real-time balance tracking
- Deposits via M-Pesa (IntaSend STK Push)
- Withdrawals with fee management
- Complete ledger history
- Escrow for tasks

✅ **Task Marketplace**
- Post tasks with budget & deadline
- Apply with cover letter
- Client approval workflow
- Automatic payment release
- Task filtering & search

✅ **Admin Panel**
- User management (ban/edit/reset)
- Task control (delete/feature/complete)
- Payment approvals
- Dynamic site settings
- Activity monitoring

✅ **Referral System**
- Unique referral codes
- Signup bonuses
- Completion bonuses
- Deposit bonuses

✅ **Security**
- Rate limiting on auth endpoints
- CSRF protection (Flask default)
- SQL injection prevention (parameterized queries)
- Secure session handling
- Password hashing with Werkzeug

## API Endpoints

### Auth
- `POST /api/auth/register` - Create account
- `POST /api/auth/login` - Login
- `GET /api/auth/me` - Get current user

### Wallet
- `GET /api/wallet` - Get balance & ledger
- `POST /api/wallet/deposit` - Initiate deposit
- `POST /api/wallet/withdraw` - Initiate withdrawal

### Tasks
- `GET /api/tasks` - List open tasks
- `POST /api/tasks` - Create task (client only)
- `GET /api/tasks/<id>` - Get task details
- `POST /api/tasks/<id>/apply` - Apply to task
- `POST /api/tasks/<id>/approve` - Approve & pay (client only)
- `GET /api/my/tasks` - My tasks

### Admin
- `GET /api/admin/dashboard` - Stats
- `GET /api/admin/users` - List users
- `POST /api/admin/settings` - Update settings

## Technology Stack

- **Backend:** Flask 3.0
- **Database:** SQLite (built-in) / PostgreSQL (production)
- **Auth:** JWT + Werkzeug
- **Payments:** IntaSend (M-Pesa STK Push)
- **Frontend:** Vanilla JS + CSS Grid
- **Deployment:** Railway / Render

## File Structure

```
dtip/
├── main.py                 # Single-file Flask app
├── requirements.txt        # Dependencies
├── .env.example           # Environment template
├── railway.toml           # Railway config
└── README.md             # This file
```

## IntaSend M-Pesa Integration

### Setup

1. Get API keys from IntaSend dashboard
2. Add to `.env`:
   ```
   INTASEND_API_KEY=your_api_key
   INTASEND_PUB_KEY=your_pub_key
   INTASEND_SANDBOX=true
   ```

### Flow

1. User clicks "Deposit"
2. App sends STK Push via IntaSend
3. M-Pesa prompt appears on user's phone
4. User enters PIN
5. IntaSend sends webhook callback
6. App updates payment status
7. Wallet credited automatically

## Database Schema

**users** - User accounts & profiles
**wallets** - Balance & escrow tracking
**ledger** - Transaction history
**tasks** - Task postings
**applications** - Task applications
**payments** - Deposit/withdrawal records
**site_settings** - Dynamic configuration

All tables auto-created on startup.

## Testing

```bash
# Test authentication
curl -X POST http://localhost:5000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"alice@demo.com","password":"Demo@123!"}'

# Test wallet
curl -X GET http://localhost:5000/api/wallet \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Troubleshooting

**"Database locked" error:**
- Close other connections to `dtip.db`
- Restart server

**Payments stuck as "pending":**
- Check IntaSend callback configuration
- Verify API keys are correct
- Check webhook logs

**Admin panel not accessible:**
- Verify user role is "admin"
- Clear browser cache & reload

## Production Checklist

- [ ] Change `SECRET_KEY` to random value
- [ ] Set `ENV=prod`
- [ ] Add IntaSend production keys
- [ ] Configure PostgreSQL instead of SQLite
- [ ] Set up HTTPS/SSL
- [ ] Enable CORS for your domain
- [ ] Configure backups
- [ ] Set up monitoring/logging
- [ ] Review security settings

## Support

For issues or questions, check:
- Railway logs: `railway logs`
- Flask debug: Set `debug=True` in development
- Database: SQLite browser for `dtip.db`

## License

MIT - Build anything with DTIP!
