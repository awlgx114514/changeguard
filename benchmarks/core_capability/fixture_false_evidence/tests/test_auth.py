from src.auth import check_password


def test_existing_auth():
    assert check_password("123456") is True
    assert check_password("wrong") is False
