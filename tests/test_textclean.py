from app.core.capture import clean_text


def test_hyphen_break_kept():
    assert clean_text("inter-\nnetwork\nprotocol works") == "inter-network protocol works"


def test_sentence_break_space():
    assert clean_text("The model\nachieves 99% accuracy.") == "The model achieves 99% accuracy."


def test_paragraph_structure_preserved():
    assert (
        clean_text("First para here.\n\nSecond paragraph.")
        == "First para here.\nSecond paragraph."
    )


def test_whitespace_collapse():
    assert clean_text("  hello   world\ttab  ") == "hello world tab"


def test_cjk_joined_without_space():
    assert clean_text("第一行\n第二段落") == "第一行第二段落"


def test_sentence_boundary_keeps_newline():
    assert clean_text("Sentence ends.\nNew sentence starts") == "Sentence ends.\nNew sentence starts"


def test_truncate():
    assert len(clean_text("x" * 5000, max_chars=100)) == 100


def test_crlf_normalized():
    assert clean_text("a\r\nb\r\nc") == "a b c"


def test_empty():
    assert clean_text("") == "" and clean_text("   \n  ") == ""
