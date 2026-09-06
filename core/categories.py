# -*- coding: utf-8 -*-
"""
categories - 林況カテゴリと表示色の定義

GUI とプロセシングの両方から参照する。ここに一本化しておかないと、
「ダイアログを開いたときだけ色がつく」という状態になる。

License: GPL v2
"""

from __future__ import annotations

from typing import List, Optional, Sequence

# 北海道の林況区分。マニュアル図27の区分に合わせた既定色。
# 針葉樹は濃い緑、広葉樹は明るい黄緑にして、印刷しても区別がつくようにしてある。
DEFAULT_CATEGORIES = [
    ("常緑針葉樹林", "#1a6b3c"),
    ("落葉針葉樹林", "#5aa17a"),
    ("落葉広葉樹林", "#8fbc4a"),
    ("針広混交林", "#4d9a63"),
    ("疎林", "#c3d68b"),
    ("草地・ササ地", "#e8d67a"),
    ("農地", "#f0c060"),
    ("市街地・道路・裸地", "#b0663f"),
    ("雲・裸地", "#e6e6e6"),
    ("水域", "#3a6fb0"),
    ("影", "#4a4a4a"),
    ("未分類", "#cccccc"),
]

COLOR_BY_NAME = dict(DEFAULT_CATEGORIES)

# ラベルが既定カテゴリに無いときの予備色。
# 隣り合うクラスが似た色にならないよう色相を散らしてある。
FALLBACK_PALETTE = [
    "#4c78a8", "#f58518", "#54a24b", "#e45756", "#72b7b2",
    "#eeca3b", "#b279a2", "#ff9da6", "#9d755d", "#bab0ac",
    "#1f6f8b", "#c96a1b", "#2f7a3d", "#a83b3b", "#4a8f8a",
    "#b09a2a", "#8a5c7a", "#c97a82", "#75563f", "#8a8280",
]


def color_for(label: Optional[str], index: int = 0) -> str:
    """ラベルに対応する色を返す。未知のラベルには予備色を割り当てる。

    index はクラス番号（0始まり）。同じクラスには必ず同じ色が返る。
    """
    if label and label in COLOR_BY_NAME:
        return COLOR_BY_NAME[label]
    return FALLBACK_PALETTE[index % len(FALLBACK_PALETTE)]


def apply_default_colors(signature, overwrite: bool = False) -> List[str]:
    """シグネチャの colors を埋める。

    overwrite=False なら、すでに色が入っているクラスはそのまま残す
    （利用者が手で選んだ色を消さないため）。
    """
    colors = list(signature.colors) if signature.colors else []
    while len(colors) < signature.n_classes:
        colors.append("")
    labels: Sequence[str] = signature.labels or []

    for i in range(signature.n_classes):
        if colors[i] and not overwrite:
            continue
        label = labels[i] if i < len(labels) else ""
        colors[i] = color_for(label, i)
    signature.colors = colors[:signature.n_classes]
    return signature.colors
