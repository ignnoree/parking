from helpers.plate_format import (
    fix_ocr_confusions,
    ocr_read_variants,
    rank_ocr_candidate,
    sanitize_plate_ocr_text,
)


def test_pipe_mapped_to_capital_i():
    assert sanitize_plate_ocr_text("AB|123") == "ABI123"
    assert fix_ocr_confusions("T|07") == "TI07"


def test_one_to_i_beside_letters():
    assert sanitize_plate_ocr_text("AB1C456") == "ABIC456"
    assert sanitize_plate_ocr_text("1ABC") == "IABC"


def test_one_to_i_in_letter_heavy_plate():
    """1 surrounded by letters in letter-heavy plates becomes I (MW51VSU -> MW5IVSU)."""
    assert sanitize_plate_ocr_text("MW51VSU") == "MW5IVSU"
    assert sanitize_plate_ocr_text("EY61NBG") == "EY6INBG"
    assert sanitize_plate_ocr_text("FJ14ZHY") == "FJI4ZHY"
    assert sanitize_plate_ocr_text("LM13VCV") == "LMI3VCV"
    assert sanitize_plate_ocr_text("GX15OGJ") == "GXI5OGJ"


def test_one_kept_in_digit_heavy_plate():
    assert sanitize_plate_ocr_text("B1234") == "B1234"
    assert sanitize_plate_ocr_text("AB1234") == "AB1234"
    assert sanitize_plate_ocr_text("TS07JS1234") == "TS07JS1234"


def test_one_kept_inside_long_digit_run():
    """1 inside a 3+ digit run is treated as a real digit even if plate is letter-heavy."""
    assert sanitize_plate_ocr_text("AB|123") == "ABI123"
    assert sanitize_plate_ocr_text("ABI125") == "ABI125"


def test_zero_o_by_context():
    assert sanitize_plate_ocr_text("AB0CD") == "ABOCD"
    assert sanitize_plate_ocr_text("12O34") == "12034"


def test_d_o_variants():
    variants = set(ocr_read_variants("BDB4668"))
    assert "BDB4668" in variants
    assert "BOB4668" in variants


def test_rank_prefers_longer_at_similar_confidence():
    short = rank_ocr_candidate("ABC123", 0.80)
    long = rank_ocr_candidate("ABCI123", 0.80)
    assert long > short
