import pytest
from werkzeug.security import generate_password_hash, check_password_hash


class TestPasswordHashing:
    def test_generated_hash_validates(self):
        password = "test-password-123"
        hashed = generate_password_hash(password)
        assert check_password_hash(hashed, password) is True

    def test_wrong_password_fails(self):
        hashed = generate_password_hash("correct-password")
        assert check_password_hash(hashed, "wrong-password") is False

    def test_empty_password(self):
        hashed = generate_password_hash("")
        assert check_password_hash(hashed, "") is True
        assert check_password_hash(hashed, "x") is False

    def test_default_admin_password(self):
        import os
        password = os.getenv("PASSWORD", "admin")
        hashed = generate_password_hash(password)
        assert check_password_hash(hashed, password) is True
