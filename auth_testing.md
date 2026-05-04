# Auth Testing Playbook (Emergent Google OAuth)

## Test User Setup
Run via mongosh or python:
```bash
python3 -c "
from pymongo import MongoClient
from datetime import datetime, timedelta, timezone
import uuid
db = MongoClient('mongodb://localhost:27017')['ice_schedule_ops']

uid = 'test-user-' + str(int(datetime.now().timestamp()))
sess = 'test_session_' + str(int(datetime.now().timestamp()))
tenant = db.tenants.find_one({'slug':'ice'})
db.users.insert_one({
  'id': uid,
  'email': f'test.{uid}@example.com',
  'full_name': 'Test User',
  'picture': None,
  'tenant_id': tenant['id'],
  'role': 'pm',
  'auth_provider': 'google',
  'created_at': datetime.now(timezone.utc).isoformat()
})
db.tenant_users.insert_one({
  'id': str(uuid.uuid4()),
  'user_id': uid, 'tenant_id': tenant['id'],
  'role': 'pm', 'status':'active',
  'created_at': datetime.now(timezone.utc).isoformat()
})
db.user_sessions.insert_one({
  'user_id': uid,
  'session_token': sess,
  'expires_at': (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
  'created_at': datetime.now(timezone.utc).isoformat()
})
print('user_id:', uid)
print('session_token:', sess)
"
```

## Backend test (cookie-based)
```bash
SESS=<session_token from above>
curl -X GET "$REACT_APP_BACKEND_URL/api/auth/me" -H "Cookie: session_token=$SESS"
```

## Browser test
```python
await page.context.add_cookies([{
    "name": "session_token",
    "value": "<session_token>",
    "domain": "<your-domain>",
    "path": "/",
    "httpOnly": True, "secure": True, "sameSite": "None"
}])
await page.goto("https://<your-domain>/")
```

## Cleanup
```bash
mongosh ice_schedule_ops --eval "
db.users.deleteMany({email: /^test\\./});
db.user_sessions.deleteMany({session_token: /^test_session_/});
db.tenant_users.deleteMany({user_id: /^test-user-/});
"
```

## Allowed Google identities for testing
- Any Google account works (no domain allowlist). On first login the user is auto-provisioned into the `ice` tenant with role `pm`. To test as admin, manually update `users.role` after first login, or sign in with the seeded `admin@iceops.com` (JWT email/password path).

## Flow expectations
1. User clicks "Sign in with Google" → redirects to `https://auth.emergentagent.com/?redirect=<dynamic origin>/auth/callback`
2. After Google, lands at `<origin>/auth/callback#session_id=<token>`
3. Frontend reads URL fragment, POSTs to `/api/auth/google/session` with the session_id
4. Backend exchanges via `https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data`
5. Backend creates/updates user, inserts row in `user_sessions`, sets `session_token` httpOnly cookie
6. Frontend redirects to `/`
7. All subsequent requests are authenticated via cookie OR Bearer JWT (legacy)
