"""Keys never reach the log or remarks (mnema/llm/redact.py): model and recognition keys from
~/.config/mnema/.env and the process environment, tokens and key-like strings."""

from __future__ import annotations

import logging

import pytest

from mnema import config
from mnema.live import remarks
from mnema.live.remarks import Step
from mnema.llm import claude, providers, redact
from mnema.llm.errors import human_reason

# keys without a recognizable prefix: only knowing they are keys can hide them
MODEL_KEY = "mk" + "m" * 30 + "a1B2"
ASR_KEY = "ak" + "s" * 30 + "c3D4"


@pytest.fixture
def env_keys(monkeypatch):
    """Model and recognition keys as .env lines, not in the process environment."""
    for name in ("MNEMA_MODEL", "MNEMA_API_KEY", "MNEMA_ASR_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    path = config.user_env_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"MNEMA_MODEL=deepseek/deepseek-chat\nMNEMA_API_KEY={MODEL_KEY}\n"
        f"MNEMA_ASR_API_KEY={ASR_KEY}\n"
    )
    return path


def test_redact_hides_env_file_keys_and_key_like_strings(env_keys, monkeypatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_" + "t" * 30 + "zz99")
    text = (
        f"ошибка {MODEL_KEY} и {ASR_KEY} url https://x?key=abcdefghijklmnopqrstuvwxyz123 "
        f"hf {'hf_' + 't' * 30 + 'zz99'} и {'sk-' + 'u' * 25} и {'AIza' + 'q' * 35} "
        f"и {'gsk_' + 'g' * 40}"
    )
    out = redact.redact(text)
    assert MODEL_KEY not in out and "…a1B2" in out and ASR_KEY not in out and "…c3D4" in out
    assert "abcdefghijklmnop" not in out and "tttttttt" not in out
    assert "uuuuuuuu" not in out and "qqqqqqqq" not in out and "gggggggg" not in out
    assert redact.redact("") == ""
    new_key = "nk" + "n" * 30 + "e5F6"
    env_keys.write_text(f"MNEMA_API_KEY={new_key}\n")  # mnema model changed the key
    assert new_key not in redact.redact(f"ошибка {new_key}")


@pytest.mark.parametrize("prefix", [prefix for prefix, _ in providers.KEY_PREFIXES])
def test_every_provider_key_prefix_is_hidden_in_the_log(prefix, caplog) -> None:
    """A key that is neither in the environment nor in .env is hidden by its prefix: there is one
    table of provider key prefixes, so a new provider there is hidden in the log right away."""
    key = prefix + "Zx9" * 10 + "k7Q2"
    redact.install_redaction()
    with caplog.at_level(logging.WARNING, logger="mnema.test.redact"):
        logging.getLogger("mnema.test.redact").warning("провайдер ответил: ключ %s неверен", key)
    assert key not in caplog.text and "…k7Q2" in caplog.text


def test_log_record_with_key_in_exception_is_redacted(env_keys, caplog) -> None:
    redact.install_redaction()
    redact.install_redaction()  # calling it again does nothing
    log = logging.getLogger("mnema.test.redact")
    with caplog.at_level(logging.WARNING, logger="mnema.test.redact"):
        try:
            raise RuntimeError(f"401 for url ?api_key={ASR_KEY}")
        except RuntimeError:
            log.warning("ключ %s не подошёл", MODEL_KEY, exc_info=True)
    assert MODEL_KEY not in caplog.text and ASR_KEY not in caplog.text
    assert "…a1B2" in caplog.text and "…c3D4" in caplog.text


def test_redaction_keeps_log_args_without_secrets_and_hides_env_secrets(monkeypatch) -> None:
    """A record without a secret stays as is (formatters like uvicorn read args); the runner token,
    the DeepL key and model keys from the process environment are known secrets."""
    redact.install_redaction()
    make = logging.getLogRecordFactory()
    plain = make("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s"', ("1.2.3.4", "GET", "/"),
                 None)  # fmt: skip
    assert plain.args == ("1.2.3.4", "GET", "/") and plain.getMessage() == '1.2.3.4 - "GET /"'
    token, deepl = "t" * 60 + "bZ9x", "d" * 30 + "-c0de:fx"
    monkeypatch.setenv("MNEMA_TOKEN", token)
    monkeypatch.setenv("DEEPL_API_KEY", deepl)
    monkeypatch.setenv("MNEMA_ASR_API_KEY", ASR_KEY)
    url = f"https://runner/api/jobs?token={token} и {deepl} и {ASR_KEY}"
    hidden = make("httpx", logging.ERROR, "", 0, "нет связи: %s", (url,), None)
    for secret in (token, deepl, ASR_KEY):
        assert secret not in hidden.getMessage()
    assert "…bZ9x" in hidden.getMessage()


def test_human_reason_hides_a_key_cut_by_the_length_limit(env_keys) -> None:
    """A key at the reason's cut-off (120 characters) does not leak even partly."""
    stranger = "AIza" + "z" * 35
    for secret in (MODEL_KEY, stranger):
        reason = human_reason(RuntimeError("x" * 100 + f" key={secret} конец"))
        assert secret[:12] not in reason, reason


def test_model_failure_remark_has_no_key(env_keys, monkeypatch) -> None:
    """The model answered with an error containing the key: neither the note nor the remark (also
    kept in the processing history) contains it."""
    import litellm

    def completion(**kw):
        assert kw["api_key"] == MODEL_KEY
        raise RuntimeError(f"Provider said: bad request for {MODEL_KEY}")

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(claude.time, "sleep", lambda s: None)
    title, engine, note = claude.title_ex("mic: обсудили релиз")
    assert (title, engine) == ("", "none") and note is not None
    remark = remarks.light(Step.TITLE, note)
    assert remark is not None
    shown = " ".join([note.cloud_detail, remark.text, remark.model_dump_json()])
    assert MODEL_KEY not in shown and "…a1B2" in note.cloud_detail
