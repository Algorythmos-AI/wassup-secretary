from voice_gateway.signature import sign, verify

BODY = b'{"event":"call_started","call":{"call_id":"call_test"}}'
NOW = 1_790_000_000_000


def test_valid_signature_with_current_key() -> None:
    assert verify(BODY, sign(BODY, "new", NOW), ["new"], now_ms=NOW)


def test_previous_key_accepted_during_rotation() -> None:
    assert verify(BODY, sign(BODY, "old", NOW), ["new", "old"], now_ms=NOW)


def test_unknown_key_rejected() -> None:
    assert not verify(BODY, sign(BODY, "attacker", NOW), ["new", "old"], now_ms=NOW)


def test_body_tampering_rejected() -> None:
    header = sign(BODY, "new", NOW)
    assert not verify(BODY.replace(b"call_test", b"call_evil"), header, ["new"], now_ms=NOW)


def test_reserialised_json_is_not_the_raw_body() -> None:
    header = sign(BODY, "new", NOW)
    assert not verify(
        b'{"event": "call_started", "call": {"call_id": "call_test"}}', header, ["new"], now_ms=NOW
    )


def test_replay_outside_window_rejected() -> None:
    header = sign(BODY, "new", NOW)
    assert not verify(BODY, header, ["new"], now_ms=NOW + 301_000)
    assert verify(BODY, header, ["new"], now_ms=NOW + 299_000)


def test_malformed_or_missing_header_rejected() -> None:
    for header in (None, "", "garbage", "v=abc,d=00", "d=00"):
        assert not verify(BODY, header, ["new"], now_ms=NOW)


def test_no_keys_configured_rejects_everything() -> None:
    assert not verify(BODY, sign(BODY, "new", NOW), [], now_ms=NOW)


def test_hostile_headers_are_rejected_not_crashed() -> None:
    """Values that once raised (and became 500s): non-ASCII digits and digests."""
    good = sign(BODY, "new", NOW).split(",d=")[1]
    for header in (
        "v=²³,d=" + good,  # isdigit() is true, int() raises
        f"v={NOW},d=" + "é" * 64,  # compare_digest raises on non-ASCII
        f"v={NOW},d=" + good[:-1] + "٣",  # Arabic-Indic digit
        f"v={NOW},d=" + good + "00",  # wrong length
        "v=" + "9" * 400 + ",d=" + good,  # absurd timestamp
        f" v={NOW} , d={good.upper()} ",  # whitespace and upper case are tolerated…
    ):
        result = verify(BODY, header, ["new"], now_ms=NOW)
        assert result is (header.startswith(" v=")), header  # …and only that one verifies
