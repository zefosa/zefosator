import pytest

from zefosator.errors import ConfigError
from zefosator.templating import render


def test_renders_variables_and_filters() -> None:
    result = render("el{{ v | replace('.', '_') }}", {"v": "9.8"}, where="x")
    assert result == "el9_8"


def test_inline_condition_with_else() -> None:
    template = "https://{{ 'raw.' if os_major == 9 else '' }}repo"
    assert render(template, {"os_major": 9}, where="x") == "https://raw.repo"
    assert render(template, {"os_major": 10}, where="x") == "https://repo"


def test_undefined_variable_names_location() -> None:
    with pytest.raises(
        ConfigError, match=r"vendors/a\.yaml: image\.base: .*'missing' is undefined"
    ):
        render("{{ missing }}", {}, where="vendors/a.yaml: image.base")


def test_inline_condition_without_else_renders_empty() -> None:
    assert render("{{ 'raw.' if os_major == 9 }}repo", {"os_major": 10}, where="x") == "repo"


def test_syntax_error_names_location() -> None:
    with pytest.raises(ConfigError, match="here"):
        render("{{ unclosed", {}, where="here")


def test_sandbox_blocks_unsafe_attribute_access() -> None:
    with pytest.raises(ConfigError, match="here"):
        render("{{ ''.__class__.__mro__ }}", {}, where="here")
