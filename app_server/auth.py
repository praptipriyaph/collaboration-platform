import hashlib
import secrets
from datetime import datetime, timedelta

class AuthManager:
    def __init__(self):
        self.users = {
            "admin": self._hash_password("admin123"),
            "user1": self._hash_password("password1"),
            "user2": self._hash_password("password2"),
        }
        self.sessions = {}
        self.session_timeout = timedelta(hours=1)
    
    def _hash_password(self, password):
        return hashlib.sha256(password.encode()).hexdigest()
    
    def authenticate(self, username, password):
        if username in self.users:
            if self.users[username] == self._hash_password(password):
                token = secrets.token_hex(16)
                self.sessions[token] = {
                    "username": username,
                    "created": datetime.now()
                }
                return True, token
        return False, None
    
    def validate_token(self, token):
        if token in self.sessions:
            session = self.sessions[token]
            if datetime.now() - session["created"] < self.session_timeout:
                return True, session["username"]
            else:
                del self.sessions[token]
        return False, None
    
    def logout(self, token):
        if token in self.sessions:
            del self.sessions[token]
            return True
        return False

    def apply_create_session(self, token, username):
        if token not in self.sessions:
            self.sessions[token] = {
                "username": username,
                "created": datetime.now()
            }
            return True
        return False

    def apply_delete_session(self, token):
        if token in self.sessions:
            del self.sessions[token]
            return True
        return False

    def get_state(self):
        # Convert datetime objects to strings for JSON serialization
        sessions_serializable = {
            token: {
                "username": data["username"],
                "created": data["created"].isoformat()
            } for token, data in self.sessions.items()
        }
        return {'sessions': sessions_serializable}

    def load_state(self, state):
        sessions_raw = state.get('sessions', {})
        # Convert string timestamps back to datetime objects
        self.sessions = {
            token: {
                "username": data["username"],
                "created": datetime.fromisoformat(data["created"])
            } for token, data in sessions_raw.items()
        }
        print("✅ DEBUG: AuthManager state loaded from snapshot.")