import pytest

from pysqlbridge.credentials import Credential, credential, resolve
from pysqlbridge.source import SourceError


class TestResolving:
    def test_a_reference_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("PYSQLBRIDGE_TEST_TOKEN", "abc123")
        assert resolve("${PYSQLBRIDGE_TEST_TOKEN}", what="auth") == "abc123"

    def test_the_env_prefix_means_the_same_thing(self, monkeypatch):
        monkeypatch.setenv("PYSQLBRIDGE_TEST_TOKEN", "abc123")
        assert resolve("${env:PYSQLBRIDGE_TEST_TOKEN}", what="auth") == "abc123"

    def test_a_reference_can_sit_inside_a_larger_string(self, monkeypatch):
        monkeypatch.setenv("PYSQLBRIDGE_TEST_TOKEN", "abc123")
        assert resolve("v1-${PYSQLBRIDGE_TEST_TOKEN}", what="auth") == "v1-abc123"

    def test_a_literal_passes_through(self):
        assert resolve("plain", what="auth") == "plain"

    def test_an_unset_variable_is_named(self, monkeypatch):
        monkeypatch.delenv("PYSQLBRIDGE_ABSENT", raising=False)
        with pytest.raises(SourceError, match="PYSQLBRIDGE_ABSENT"):
            resolve("${PYSQLBRIDGE_ABSENT}", what="auth")


class TestPlacement:
    def test_bearer_goes_in_the_authorization_header(self):
        url, headers = credential({"bearer": "t"}).apply("https://x.test/a", {})
        assert url == "https://x.test/a"
        assert headers == {"Authorization": "Bearer t"}

    def test_basic_is_base64_of_user_and_password(self):
        _, headers = credential(
            {"basic": {"username": "u", "password": "p"}}
        ).apply("https://x.test", {})
        assert headers == {"Authorization": "Basic dTpw"}

    def test_a_key_goes_in_the_named_header(self):
        _, headers = credential({"header": "X-API-Key", "value": "k"}).apply(
            "https://x.test", {}
        )
        assert headers == {"X-API-Key": "k"}

    def test_a_prefix_is_put_in_front_of_the_key(self):
        _, headers = credential(
            {"header": "Authorization", "prefix": "Token", "value": "k"}
        ).apply("https://x.test", {})
        assert headers == {"Authorization": "Token k"}

    def test_a_key_goes_in_the_query_string(self):
        url, headers = credential({"query": "api_key", "value": "k"}).apply(
            "https://x.test/a", {}
        )
        assert url == "https://x.test/a?api_key=k"
        assert headers == {}

    def test_an_existing_query_string_is_kept(self):
        url, _ = credential({"query": "api_key", "value": "k"}).apply(
            "https://x.test/a?limit=5", {}
        )
        assert url == "https://x.test/a?limit=5&api_key=k"

    def test_the_same_parameter_is_replaced_rather_than_repeated(self):
        url, _ = credential({"query": "api_key", "value": "new"}).apply(
            "https://x.test/a?api_key=old", {}
        )
        assert url == "https://x.test/a?api_key=new"

    def test_existing_headers_are_kept(self):
        _, headers = credential({"bearer": "t"}).apply(
            "https://x.test", {"Accept": "application/json"}
        )
        assert headers["Accept"] == "application/json"

    def test_the_headers_passed_in_are_not_modified(self):
        # Mirrors of one source are raced on several threads with one headers
        # dict between them. A credential that wrote into it would be a race.
        original = {"Accept": "application/json"}
        credential({"bearer": "t"}).apply("https://x.test", original)
        assert original == {"Accept": "application/json"}


class TestRefusing:
    def test_two_schemes_is_an_error(self):
        with pytest.raises(SourceError, match="exactly one"):
            credential({"bearer": "t", "query": "k"})

    def test_no_scheme_is_an_error(self):
        with pytest.raises(SourceError, match="exactly one"):
            credential({"token": "t"})

    def test_a_header_without_a_value_is_an_error(self):
        with pytest.raises(SourceError, match='needs a "value"'):
            credential({"header": "X-API-Key"})

    def test_basic_without_a_password_is_an_error(self):
        with pytest.raises(SourceError, match="username and a password"):
            credential({"basic": {"username": "u"}})

    def test_a_string_is_not_a_credential(self):
        with pytest.raises(SourceError, match="must be an object"):
            credential("token")

    def test_nothing_configured_is_no_credential(self):
        assert credential(None) is None

    def test_an_empty_secret_is_refused(self):
        with pytest.raises(SourceError, match="empty"):
            Credential("bearer", "")


class TestDisclosure:
    def test_the_secret_is_not_in_the_repr(self):
        # These reach tracebacks, log lines and debugger views. A token in any
        # of those has to be treated as leaked.
        assert "hunter2" not in repr(credential({"bearer": "hunter2"}))

    def test_the_secret_is_not_in_the_description(self):
        assert "hunter2" not in str(credential({"bearer": "hunter2"}))

    def test_the_description_names_the_variable_it_came_from(self, monkeypatch):
        monkeypatch.setenv("PYSQLBRIDGE_TEST_TOKEN", "hunter2")
        described = str(credential({"bearer": "${PYSQLBRIDGE_TEST_TOKEN}"}))
        assert "PYSQLBRIDGE_TEST_TOKEN" in described
        assert "hunter2" not in described

    def test_a_literal_is_described_without_being_shown(self):
        described = str(credential({"header": "X-API-Key", "value": "hunter2"}))
        assert "a literal in the configuration" in described
        assert "hunter2" not in described
