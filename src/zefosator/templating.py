from collections.abc import Mapping

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from zefosator.errors import ConfigError

_ENV = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)


def render(template: str, variables: Mapping[str, object], *, where: str) -> str:
    """Render one YAML string value; errors name the file and key in `where`."""
    try:
        return _ENV.from_string(template).render(variables)
    except TemplateError as exc:
        msg = f"{where}: {exc}"
        raise ConfigError(msg) from exc
