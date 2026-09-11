"""Who may connect with a username and a password.

The rules worth holding onto are the security ones: nothing is admitted
unless the configuration named it, an unknown name and a wrong password are
the same answer at the same cost, and no secret reaches a repr.
"""

import time

import pytest

from pysqlbridge.logins import (
    ITERATIONS,
    Login,
    LoginStore,
    from_document,
    hash_password,
    load,
)
from pysqlbridge.source import SourceError

STORED = hash_password("hunter2")


class TestHashingAPassword:
    def test_it_names_its_algorithm_and_cost(self):
        algorithm, iterations, _salt, _digest = STORED.split("$")
        assert algorithm == "pbkdf2_sha256"
        assert int(iterations) == ITERATIONS

    def test_the_password_is_not_in_it(self):
        assert "hunter2" not in STORED

    def test_the_same_password_hashes_differently_each_time(self):
        # A per-password salt, so two accounts that chose the same password
        # do not look the same in a file somebody reads.
        assert hash_password("hunter2") != STORED

    def test_the_same_salt_reproduces_it(self):
        salted = hash_password("hunter2", salt=b"0123456789abcdef")
        assert salted == hash_password("hunter2", salt=b"0123456789abcdef")

    def test_an_empty_password_is_refused(self):
        with pytest.raises(SourceError, match="cannot be empty"):
            hash_password("")


class TestOneLogin:
    def test_a_hash_admits_its_password(self):
        assert Login(user="reader", secret=STORED, hashed=True).admits("hunter2")

    def test_and_refuses_another(self):
        assert not Login(user="reader", secret=STORED, hashed=True).admits("nope")

    def test_a_password_admits_itself(self):
        assert Login(user="reader", secret="hunter2", hashed=False).admits("hunter2")

    def test_the_secret_stays_out_of_the_repr(self):
        # These reach tracebacks and debugger views, and a password that
        # reaches either has to be treated as disclosed.
        held = Login(user="reader", secret="hunter2", hashed=False)
        assert "hunter2" not in repr(held)
        assert "hunter2" not in str(held)


class TestTheStore:
    def store(self):
        return LoginStore([Login(user="reader", secret=STORED, hashed=True)])

    def test_the_right_password_is_admitted(self):
        assert self.store().admits("reader", "hunter2")

    def test_a_wrong_password_is_not(self):
        assert not self.store().admits("reader", "nope")

    def test_an_unknown_user_is_not(self):
        assert not self.store().admits("nobody", "hunter2")

    def test_a_name_is_matched_without_regard_to_case(self):
        assert self.store().admits("READER", "hunter2")

    def test_an_empty_store_admits_nobody(self):
        # Failing closed: a bridge nobody configured for SQL logins refuses
        # every password there is rather than admitting any.
        assert not LoginStore().configured
        assert not LoginStore().admits("reader", "hunter2")

    def test_it_lists_names_and_never_secrets(self):
        assert self.store().names == ["reader"]

    def test_an_unknown_name_costs_what_a_known_one_does(self):
        # Otherwise the time taken says whether the name exists, and a name
        # that exists is worth guessing a password for. The decoy is what
        # makes the two the same work.
        store = self.store()

        def took(user):
            start = time.perf_counter()
            store.admits(user, "wrong")
            return time.perf_counter() - start

        known = min(took("reader") for _ in range(3))
        unknown = min(took("nobody") for _ in range(3))
        assert max(known, unknown) / min(known, unknown) < 3.0


class TestReadingAConfiguration:
    def test_no_logins_section_is_no_logins(self):
        assert not from_document({}, "'test'").configured

    def test_a_password_may_come_from_the_environment(self, monkeypatch):
        # A config file gets committed, so it holds the variable's name and
        # the value stays outside it.
        monkeypatch.setenv("PYSQLBRIDGE_TEST_PW", "from-the-environment")
        store = from_document(
            {"logins": [{"user": "reader",
                         "password": "${env:PYSQLBRIDGE_TEST_PW}"}]},
            "'test'",
        )
        assert store.admits("reader", "from-the-environment")

    def test_a_variable_that_is_not_set_is_named(self, monkeypatch):
        monkeypatch.delenv("PYSQLBRIDGE_NOT_SET", raising=False)
        with pytest.raises(SourceError, match="PYSQLBRIDGE_NOT_SET"):
            from_document(
                {"logins": [{"user": "a",
                             "password": "${env:PYSQLBRIDGE_NOT_SET}"}]},
                "'test'",
            )

    def test_a_stored_hash_is_read(self):
        store = from_document(
            {"logins": [{"user": "reader", "password_hash": STORED}]}, "'test'"
        )
        assert store.admits("reader", "hunter2")

    def test_a_misspelled_option_is_refused(self):
        with pytest.raises(SourceError, match="not a login option"):
            from_document({"logins": [{"user": "a", "pasword": "x"}]}, "'test'")

    def test_a_login_with_no_password_is_refused(self):
        with pytest.raises(SourceError, match="password_hash"):
            from_document({"logins": [{"user": "a"}]}, "'test'")

    def test_a_login_with_no_user_is_refused(self):
        with pytest.raises(SourceError, match='needs a "user"'):
            from_document({"logins": [{"password": "x"}]}, "'test'")

    def test_both_a_password_and_a_hash_is_refused(self):
        with pytest.raises(SourceError, match="it can have one"):
            from_document(
                {"logins": [{"user": "a", "password": "x",
                             "password_hash": STORED}]}, "'test'"
            )

    def test_two_logins_of_one_name_are_refused(self):
        with pytest.raises(SourceError, match="both called"):
            from_document(
                {"logins": [{"user": "a", "password": "x"},
                            {"user": "A", "password": "y"}]}, "'test'"
            )

    def test_an_unreadable_hash_is_refused_at_startup(self):
        # Rather than as a refusal later that nobody can explain.
        with pytest.raises(SourceError, match="not readable"):
            from_document(
                {"logins": [{"user": "a", "password_hash": "not-a-hash"}]},
                "'test'",
            )

    def test_a_hash_of_an_algorithm_this_does_not_know(self):
        with pytest.raises(SourceError, match="understands only"):
            from_document(
                {"logins": [{"user": "a", "password_hash": "bcrypt$1$a$b"}]},
                "'test'",
            )

    def test_logins_must_be_an_array(self):
        with pytest.raises(SourceError, match="must be an array"):
            from_document({"logins": {"user": "a"}}, "'test'")


class TestTheHashPasswordCommand:
    """--hash-password, which is how a hash gets into a configuration.

    It asks for the password rather than taking it as an argument, because a
    password written on a command line is in the shell's history and in the
    process list while it runs. That also means it cannot be driven by piping
    to it: on Windows getpass reads the console directly and ignores stdin,
    so answering the prompt is what a test has to do.
    """

    @staticmethod
    def answering(monkeypatch, *replies):
        import getpass

        given = list(replies)
        monkeypatch.setattr(getpass, "getpass", lambda prompt="": given.pop(0))

    def test_it_prints_a_hash_that_admits_the_password(self, monkeypatch,
                                                       capsys):
        from pysqlbridge.server import _print_password_hash

        self.answering(monkeypatch, "hunter2", "hunter2")
        _print_password_hash()

        printed = capsys.readouterr().out.strip()
        assert Login(user="reader", secret=printed, hashed=True).admits("hunter2")
        assert "hunter2" not in printed

    def test_it_stops_when_the_two_do_not_match(self, monkeypatch):
        from pysqlbridge.server import _print_password_hash

        self.answering(monkeypatch, "hunter2", "hunter3")
        with pytest.raises(SystemExit, match="did not match"):
            _print_password_hash()

    def test_it_stops_on_an_empty_password(self, monkeypatch):
        from pysqlbridge.server import _print_password_hash

        self.answering(monkeypatch, "", "")
        with pytest.raises(SystemExit, match="cannot be empty"):
            _print_password_hash()


class TestLoadingAFile:
    def test_a_file_with_logins(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(
            '{"logins": [{"user": "reader", "password_hash": "' + STORED + '"}]}',
            encoding="utf-8",
        )
        assert load(path).admits("reader", "hunter2")

    def test_a_file_that_is_not_there(self, tmp_path):
        with pytest.raises(SourceError, match="could not read"):
            load(tmp_path / "missing.json")

    def test_a_file_that_is_not_json(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("not json", encoding="utf-8")
        with pytest.raises(SourceError, match="not valid JSON"):
            load(path)
