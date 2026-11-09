"""Settings that have a built-in default still obey the environment (.env), and the Settings page wins over both."""
import pytest
from studio import settings


@pytest.mark.parametrize('name,raw,expected', [('STORAGE_DRIVER', 's3', 's3'), ('FETCH_MAX_MB', '40', 40), ('DUPLICATE_SIMILARITY', '0.8', 0.8),
                                               ('S3_PATH_STYLE', 'true', True), ('LIBRARY_ENABLED', 'false', False), ('S3_BUCKET', 'docs', 'docs')])
def test_environment_value_is_used(monkeypatch, name, raw, expected):
    monkeypatch.setenv(name, raw)
    assert settings.get(name) == expected and settings.source(name) == 'env'


@pytest.mark.parametrize('name,raw', [('STORAGE_DRIVER', 'floppy'), ('FETCH_MAX_MB', 'lots'), ('FETCH_MAX_MB', '99999'), ('S3_PATH_STYLE', 'maybe')])
def test_a_bad_environment_value_falls_back_to_the_default(monkeypatch, name, raw):
    monkeypatch.setenv(name, raw)
    assert settings.get(name) == settings.DEFAULTS[name]


def test_unset_or_empty_environment_uses_the_default(monkeypatch):
    monkeypatch.delenv('FETCH_MAX_MB', raising=False)
    assert settings.get('FETCH_MAX_MB') == settings.DEFAULTS['FETCH_MAX_MB']
    monkeypatch.setenv('FETCH_MAX_MB', '')
    assert settings.get('FETCH_MAX_MB') == settings.DEFAULTS['FETCH_MAX_MB']
