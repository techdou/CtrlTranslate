"""popup 术语区纯函数单测：术语解析与渲染（☆ 链接）。"""

from app.ui.popup import _format_result, _parse_terms
from app.ui.theme import palette


def test_parse_terms_basic():
    text = "译文正文\n【术语】\nGAN — 生成对抗网络（一种生成模型）\ndropout — 随机失活"
    assert _parse_terms(text) == [
        ("GAN", "生成对抗网络（一种生成模型）"),
        ("dropout", "随机失活"),
    ]


def test_parse_terms_separator_variants_and_bullets():
    text = "x\n【术语】\n- A - 甲\n· B—乙\nplain line"
    terms = _parse_terms(text)
    assert terms[0] == ("A", "甲")
    assert terms[1] == ("B", "乙")
    assert terms[2] == ("plain line", "")  # 无分隔符整行当术语


def test_parse_terms_no_section():
    assert _parse_terms("没有术语段的普通译文") == []
    assert _parse_terms("") == []


def test_format_result_renders_term_links():
    p = palette("dark")
    text = "正文\n【术语】\nGAN — 生成对抗网络"
    terms = _parse_terms(text)
    html = _format_result(text, p, terms)
    assert "href='term:0'" in html          # 首行术语带收藏链接
    assert "☆" in html
    assert "点 ☆ 收藏" in html              # 操作提示
    assert "生成对抗网络" in html
    # 链接编号与解析结果对齐（收藏数据一致性）
    assert html.count("href='term:") == len(terms)


def test_format_result_without_terms_section():
    p = palette("dark")
    html = _format_result("只有正文", p)
    assert "href='term:" not in html
