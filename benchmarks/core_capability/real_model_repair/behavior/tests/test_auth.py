from src.auth import check_password


def test_known_password_is_accepted():
    assert check_password("s3cure-pass") is True


def test_wrong_password_is_rejected():
    assert check_password("incorrect") is False
