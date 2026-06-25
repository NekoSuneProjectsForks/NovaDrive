from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
# Per-client throttle for credential-guessing surfaces (login, 2FA, password
# reset, email resend). Keyed on the real client IP (ProxyFix sets remote_addr
# from X-Forwarded-For). In-memory storage suits a single waitress process; set
# RATELIMIT_STORAGE_URI (e.g. redis://) for multi-process deployments.
limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")

