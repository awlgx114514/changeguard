from src.calculator import calculate_total


def test_empty_total_is_zero():
    assert calculate_total([]) == 0


def test_total_is_the_sum_of_all_items():
    assert calculate_total([1, 2, 3]) == 6
