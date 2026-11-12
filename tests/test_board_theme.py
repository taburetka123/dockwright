import pytest

from dockwright import board_theme


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_pairs_reach_4_5_and_control_pairs_reach_3(theme):
    colors = board_theme.TOKENS[theme]
    low_text = [p for p in board_theme.TEXT_PAIRS if board_theme.ratio(colors[p[0]], colors[p[1]]) < 4.5]
    low_ui = [p for p in board_theme.UI_PAIRS if board_theme.ratio(colors[p[0]], colors[p[1]]) < 3.0]
    assert low_text == [] and low_ui == []


def test_the_ratio_is_the_wcag_formula():
    assert board_theme.ratio("#ffffff", "#000000") == pytest.approx(21.0)
    assert board_theme.ratio("#767676", "#ffffff") == pytest.approx(4.54, abs=0.01)
    assert board_theme.ratio("#777777", "#ffffff") < 4.5


def test_the_stylesheet_declares_every_token_of_both_themes():
    assert set(board_theme.TOKENS["light"]) == set(board_theme.TOKENS["dark"])
    light, dark = board_theme.css().split("@media (prefers-color-scheme: dark)", 1)
    for name, value in board_theme.TOKENS["light"].items():
        assert f"--{name}:{value}" in light
    for name, value in board_theme.TOKENS["dark"].items():
        assert f"--{name}:{value}" in dark
