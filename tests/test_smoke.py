from zefosator.errors import BuildError, ConfigError, ZefosatorError


def test_errors_share_a_base_class() -> None:
    assert issubclass(ConfigError, ZefosatorError)
    assert issubclass(BuildError, ZefosatorError)
