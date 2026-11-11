"""A web message folded back to what the person wrote, for titles."""

from __future__ import annotations

from kimo_cli.utils.string import fold_message_markup


def test_a_paste_becomes_a_placeholder_where_it_was():
    text = '帮我看看这段订单数据：<pasted_text index="1">order_id,date\r\n1,2</pasted_text>，重点看金额。'

    assert fold_message_markup(text) == "帮我看看这段订单数据：⟦…⟧，重点看金额。"


def test_files_before_the_message_leave_only_their_names():
    text = (
        "<uploaded_files>\n1. /Users/a/uploads/x.h\n</uploaded_files>\n\n"
        ' <document path="/Users/a/uploads/x.h" content_type="text/x-c"> #ifndef X </document>\n\n'
        " 看下这个头文件"
    )

    folded = " ".join(fold_message_markup(text).split())

    assert folded == "⟦x.h⟧ 看下这个头文件"
    assert "/Users/a" not in folded


def test_files_placed_in_the_sentence_keep_their_place():
    text = (
        '对比 <image path="C:\\u\\uploads\\shot.png" inline="1"> </image>'
        ' 和 <file path="/u/uploads/a.zip" inline="1"/>'
    )

    assert fold_message_markup(text) == "对比 ⟦shot.png⟧ 和 ⟦a.zip⟧"


def test_plain_text_is_untouched():
    assert fold_message_markup("按月汇总 orders.csv 的销售额") == "按月汇总 orders.csv 的销售额"
