from src.auth import check_password


def test_existing_password_behavior():
    assert check_password("123456") is True
