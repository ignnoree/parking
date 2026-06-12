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
