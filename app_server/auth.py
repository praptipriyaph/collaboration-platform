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