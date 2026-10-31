"""Server users: names, the user of the current context and where their data lives."""

from pathlib import Path

import pytest

from mnema import users
from mnema.core.base import MediaError


def test_a_user_name_is_lowercase_latin_up_to_32():
    assert users.valid_name("Anna") == "anna"
    assert users.valid_name("a" * 32) == "a" * 32
    assert users.valid_name("bob_2-x") == "bob_2-x"
    for raw in ("owner", "Owner", "-a", "_a", "a" * 33, "анна", "", "a b", "a\n", "a/b"):
        with pytest.raises(MediaError, match="латинские буквы"):
            users.valid_name(raw)


def test_as_user_nests_and_restores():
    assert users.current_user() is None
    with users.as_user("anna"):
        assert users.current_user() == "anna"
        with users.as_user(users.OWNER):
            assert users.current_user() == "owner"
        assert users.current_user() == "anna"
    assert users.current_user() is None


def test_the_owner_keeps_the_old_library_and_users_live_beside_it(tmp_path):
    root = Path(tmp_path)
    assert users.user_library(root, "owner") == root / "library"
    assert users.user_library(root, "anna") == root / "users" / "anna" / "library"
    assert users.user_env_file("anna") == tmp_path / ".config" / "mnema" / "users" / "anna.env"
