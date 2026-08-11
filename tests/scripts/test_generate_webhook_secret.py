from __future__ import annotations

import pytest

from scripts.generate_webhook_secret import generate_secret, main, parse_args


def test_generate_secret_returns_url_safe_random_string() -> None:
    secret = generate_secret()

    assert isinstance(secret, str)
    assert len(secret) > 20


def test_generate_secret_is_random_across_calls() -> None:
    assert generate_secret() != generate_secret()


def test_generate_secret_respects_num_bytes() -> None:
    short = generate_secret(4)
    long = generate_secret(64)

    assert len(short) < len(long)


def test_parse_args_default_bytes() -> None:
    args = parse_args([])

    assert args.num_bytes == 32


def test_parse_args_custom_bytes() -> None:
    args = parse_args(["--bytes", "48"])

    assert args.num_bytes == 48


def test_main_prints_secret_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    main([])

    captured = capsys.readouterr()
    printed = captured.out.strip()
    assert printed
    assert len(printed) > 20
