# -*- coding: utf-8 -*-
"""
rinkyo_core - 教師なし分類（クラスタリング + 最尤法分類）のコア実装

GRASS GIS の i.cluster / i.maxlik に相当する処理を numpy だけで行う。
QGIS API に依存しないので、単体でテスト可能。
ラスタ入出力のみ osgeo.gdal を使う（QGIS には必ず同梱されている）。

License: GPL v2 (QGIS プラグインに同梱する前提)
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from typing import Callable, List, Optional, Sequence

import numpy as np

__all__ = [
    "Signature",
    "ClusterResult",
    "cluster",
    "log_likelihood",
    "classify_array",
    "suggest_labels",
    "prepare_signature",
]

# 共分散行列が特異になったときに対角へ加えるリッジ係数の初期値。
# NDVI は Band4/Band8 の関数なので、この5バンドの共分散はほぼ確実に
# 悪条件（ill-conditioned）になる。リッジ無しの実装は必ず破綻する。
_RIDGE_START = 1e-8
_RIDGE_MAX = 1e-1


# ---------------------------------------------------------------------------
# シグネチャ
# ---------------------------------------------------------------------------
@dataclass
class Signature:
    """クラスごとの平均ベクトルと共分散行列（原単位）。"""

    band_names: List[str]
    means: np.ndarray  # (k, nbands)
    covs: np.ndarray  # (k, nbands, nbands)
    counts: np.ndarray  # (k,)
    labels: List[str] = field(default_factory=list)
    colors: List[str] = field(default_factory=list)
    title: str = ""

    def __post_init__(self):
        self.means = np.asarray(self.means, dtype=np.float64)
        self.covs = np.asarray(self.covs, dtype=np.float64)
        self.counts = np.asarray(self.counts, dtype=np.int64)
        if not self.labels:
            self.labels = ["" for _ in range(self.n_classes)]
        if not self.colors:
            self.colors = ["" for _ in range(self.n_classes)]

    @property
    def n_classes(self) -> int:
        return int(self.means.shape[0])

    @property
    def n_bands(self) -> int:
        return int(self.means.shape[1])

    # -- 永続化 ------------------------------------------------------------
    def to_json(self) -> str:
        d = asdict(self)
        d["means"] = self.means.tolist()
        d["covs"] = self.covs.tolist()
        d["counts"] = self.counts.tolist()
        d["_format"] = "rinkyo-signature-1"
        return json.dumps(d, ensure_ascii=False, indent=1)

    @classmethod
    def from_json(cls, text: str) -> "Signature":
        d = json.loads(text)
        d.pop("_format", None)
        return cls(**d)

    def to_grass_sig(self) -> str:
        """GRASS i.cluster の signature ファイルに近い形式で書き出す。

        参考用（GRASS 側で読ませたい場合）。GRASS のバージョンによって
        ヘッダ行が異なるため、実運用前に実ファイルとの突き合わせが必要。
        """
        out = [self.title or "rinkyo unsupervised"]
        out.append("#" + " ".join(self.band_names))
        for i in range(self.n_classes):
            out.append("#%d %s" % (i + 1, self.labels[i] or ""))
            out.append(str(int(self.counts[i])))
            out.append(" ".join("%g" % v for v in self.means[i]))
            for r in range(self.n_bands):
                out.append(" ".join("%g" % self.covs[i, r, c]
                                    for c in range(r + 1)))
        return "\n".join(out) + "\n"


@dataclass
class ClusterResult:
    signature: Signature
    n_iterations: int
    converged: bool
    convergence: float  # 最終反復で所属が変わらなかった標本の割合(%)
    dropped: int  # 標本数不足で消えたクラス数
    merged: int  # 近すぎて統合されたクラス数


# ---------------------------------------------------------------------------
# クラスタリング（i.cluster 相当）
# ---------------------------------------------------------------------------
def _seed_means(z: np.ndarray, k: int) -> np.ndarray:
    """GRASS i.cluster と同じ考え方で、多次元対角線上に初期平均を配置する。

    乱数を使わないので、同じ入力からは必ず同じ結果が出る（再現性）。
    """
    lo = z.mean(axis=0) - z.std(axis=0)
    hi = z.mean(axis=0) + z.std(axis=0)
    t = np.linspace(0.0, 1.0, k).reshape(-1, 1)
    return lo + t * (hi - lo)


def _assign(z: np.ndarray, means: np.ndarray,
            chunk: int = 100000) -> np.ndarray:
    """最近傍クラスへ割り当て（ユークリッド距離）。

    |a-b|^2 = |a|^2 - 2ab + |b|^2 に展開して (n, k) の行列だけで済ませる。
    (n, k, nbands) を作る素直な実装はクラス数50・画素数百万で
    メモリを食い潰すので使わない。
    """
    out = np.empty(z.shape[0], dtype=np.int32)
    mm = (means ** 2).sum(axis=1)
    for s in range(0, z.shape[0], chunk):
        e = min(s + chunk, z.shape[0])
        d = mm[None, :] - 2.0 * (z[s:e] @ means.T)
        out[s:e] = np.argmin(d, axis=1)
    return out


def cluster(
    samples: np.ndarray,
    n_classes: int = 50,
    min_class_size: int = 17,
    min_separation: float = 0.0,
    max_iterations: int = 30,
    convergence: float = 98.0,
    band_names: Optional[Sequence[str]] = None,
    progress: Optional[Callable[[int, str], None]] = None,
) -> ClusterResult:
    """標本画素からクラスタリングを行い、シグネチャを返す。

    samples        : (n, nbands) の float 配列（NoData 除去済み）
    n_classes      : 初期クラス数（PDF の手順では 50）
    min_class_size : これ未満の標本しか持たないクラスは捨てる
    min_separation : 標準化空間でこの距離より近いクラス同士は統合（0で無効）
    convergence    : 所属が変わらない標本の割合がこの%を超えたら収束とみなす
    """
    x = np.asarray(samples, dtype=np.float64)
    if x.ndim != 2:
        raise ValueError("samples は (n, nbands) の2次元配列である必要があります")
    n, nb = x.shape
    if n < n_classes * max(min_class_size, 1):
        raise ValueError("標本数(%d)がクラス数(%d)に対して少なすぎます"
                         % (n, n_classes))
    if band_names is None:
        band_names = ["band%d" % (i + 1) for i in range(nb)]

    # --- 標準化 -----------------------------------------------------------
    # GRASS i.cluster は生の画素値のユークリッド距離を使う。だが Sentinel-2 の
    # 反射率(~1000-10000)と NDVI(-1〜1) を混ぜると NDVI の寄与がほぼ 0 になり、
    # 「NDVI を入れたのに効いていない」状態になる。ここでは Z 標準化してから
    # クラスタリングする（最尤法分類は線形変換に対して不変なので、
    # シグネチャは原単位で持ったままで問題ない）。
    g_mean = x.mean(axis=0)
    g_std = x.std(axis=0)
    g_std[g_std <= 0] = 1.0
    z = (x - g_mean) / g_std

    means = _seed_means(z, n_classes)
    prev = np.full(n, -1, dtype=np.int32)
    dropped = 0
    merged = 0
    conv = 0.0
    it = 0
    converged = False

    for it in range(1, max_iterations + 1):
        lab = _assign(z, means)
        conv = float((lab == prev).sum()) / n * 100.0
        prev = lab

        # 平均の更新
        k = means.shape[0]
        counts = np.bincount(lab, minlength=k)
        new_means = np.zeros_like(means)
        for i in range(k):
            if counts[i] > 0:
                new_means[i] = z[lab == i].mean(axis=0)
            else:
                new_means[i] = means[i]

        # 標本数の少ないクラスを削除
        keep = counts >= min_class_size
        if keep.sum() < 2:
            keep = counts > 0
        dropped += int(k - keep.sum())
        new_means = new_means[keep]

        # 近すぎるクラスを統合
        if min_separation > 0 and new_means.shape[0] > 2:
            alive = list(range(new_means.shape[0]))
            i = 0
            while i < len(alive):
                j = i + 1
                while j < len(alive):
                    d = np.linalg.norm(new_means[alive[i]] - new_means[alive[j]])
                    if d < min_separation:
                        new_means[alive[i]] = (new_means[alive[i]]
                                               + new_means[alive[j]]) / 2.0
                        alive.pop(j)
                        merged += 1
                    else:
                        j += 1
                i += 1
            new_means = new_means[alive]

        means = new_means
        if progress:
            progress(int(it * 100 / max_iterations),
                     "クラスタリング %d 回目 / クラス数 %d / 収束 %.1f%%"
                     % (it, means.shape[0], conv))
        if conv >= convergence:
            converged = True
            break

    # --- 最終シグネチャを原単位で計算 -------------------------------------
    lab = _assign(z, means)
    k = means.shape[0]
    o_means = np.zeros((k, nb))
    o_covs = np.zeros((k, nb, nb))
    counts = np.zeros(k, dtype=np.int64)
    for i in range(k):
        sel = x[lab == i]
        counts[i] = sel.shape[0]
        if sel.shape[0] > nb:
            o_means[i] = sel.mean(axis=0)
            o_covs[i] = np.cov(sel, rowvar=False)
        elif sel.shape[0] > 0:
            o_means[i] = sel.mean(axis=0)
            o_covs[i] = np.eye(nb) * np.var(x, axis=0).mean()
        else:
            o_means[i] = means[i] * g_std + g_mean
            o_covs[i] = np.eye(nb) * np.var(x, axis=0).mean()

    # 明るさ順に並べ替えると、年次が違ってもクラス番号の意味が近くなる
    order = np.argsort(o_means.mean(axis=1))
    sig = Signature(
        band_names=list(band_names),
        means=o_means[order],
        covs=o_covs[order],
        counts=counts[order],
    )
    return ClusterResult(sig, it, converged, conv, dropped, merged)


# ---------------------------------------------------------------------------
# 最尤法分類（i.maxlik 相当）
# ---------------------------------------------------------------------------
def prepare_signature(sig: Signature):
    """クラスごとに Cholesky 分解と log|Σ| を前計算する。

    NDVI を含む5バンド構成では共分散がほぼ特異になるため、
    分解できるまでリッジを強めながら再試行する。
    """
    linvs, logdets = [], []
    for i in range(sig.n_classes):
        cov = sig.covs[i].copy()
        scale = max(float(np.trace(cov)) / sig.n_bands, 1e-12)
        ridge = _RIDGE_START
        while True:
            try:
                c = np.linalg.cholesky(cov + np.eye(sig.n_bands) * ridge * scale)
                break
            except np.linalg.LinAlgError:
                ridge *= 10.0
                if ridge > _RIDGE_MAX:
                    c = np.linalg.cholesky(np.eye(sig.n_bands) * scale)
                    break
        # バンド数は高々十数なので、逆行列を前計算して行列積で解く。
        # scipy.linalg.solve_triangular に依存しないための措置。
        linvs.append(np.linalg.inv(c))
        logdets.append(2.0 * float(np.log(np.diag(c)).sum()))
    return linvs, logdets


def log_likelihood(x: np.ndarray, sig: Signature,
                   prepared=None) -> np.ndarray:
    """(n, nbands) の画素に対し (n, k) の対数尤度を返す。"""
    linvs, logdets = prepared if prepared else prepare_signature(sig)
    out = np.empty((x.shape[0], sig.n_classes), dtype=np.float32)
    const = sig.n_bands * math.log(2.0 * math.pi)
    for i in range(sig.n_classes):
        y = (x - sig.means[i]) @ linvs[i].T
        maha = (y ** 2).sum(axis=1)
        out[:, i] = -0.5 * (const + logdets[i] + maha)
    return out


def classify_array(x: np.ndarray, sig: Signature,
                   prepared=None,
                   reject_percentile: Optional[float] = None):
    """(n, nbands) を分類し、(クラス番号 1..k, 対数尤度) を返す。"""
    ll = log_likelihood(x, sig, prepared)
    cls = (np.argmax(ll, axis=1) + 1).astype(np.uint8)
    best = ll.max(axis=1)
    if reject_percentile is not None:
        thr = np.percentile(best, reject_percentile)
        cls[best < thr] = 0  # 0 = 未分類
    return cls, best


# ---------------------------------------------------------------------------
# 意味づけ支援
# ---------------------------------------------------------------------------
def suggest_labels(sig: Signature,
                   idx_blue=0, idx_green=1, idx_red=2, idx_nir=3,
                   idx_ndvi=None) -> List[str]:
    """各クラスの平均反射率から、林況カテゴリの初期案を返す。

    HRO 林業試験場マニュアルの Red-NIR 散布図（図28）の並びを規則にしたもの。
    あくまで初期案で、最終判断は利用者が行う前提。
    """
    m = sig.means
    red = m[:, idx_red]
    nir = m[:, idx_nir]
    blue = m[:, idx_blue]
    if idx_ndvi is not None:
        ndvi = m[:, idx_ndvi]
    else:
        ndvi = (nir - red) / np.maximum(nir + red, 1e-6)
    bright = m[:, [idx_blue, idx_green, idx_red, idx_nir]].mean(axis=1)

    # 植生クラス(NDVI>0.4)の NIR 分位で 針葉 / 混交 / 広葉 を切り分ける
    veg_nir = nir[ndvi > 0.4]
    if veg_nir.size:
        nir_lo = float(np.percentile(veg_nir, 33))
        nir_hi = float(np.percentile(veg_nir, 66))
    else:
        nir_lo = nir_hi = float(np.median(nir))

    out = []
    for i in range(sig.n_classes):
        if ndvi[i] < 0.0 and nir[i] < np.percentile(nir, 20):
            out.append("水域")
        elif bright[i] < np.percentile(bright, 5):
            out.append("影")
        elif ndvi[i] < 0.15 and blue[i] > np.percentile(blue, 90):
            out.append("雲・裸地")
        elif ndvi[i] < 0.3:
            out.append("市街地・道路・裸地")
        elif ndvi[i] < 0.4:
            out.append("疎林")
        elif red[i] > np.percentile(red, 75) and nir[i] > nir_hi:
            out.append("草地・ササ地")
        elif nir[i] <= nir_lo:
            out.append("常緑針葉樹林")
        elif nir[i] <= nir_hi:
            out.append("針広混交林")
        else:
            out.append("落葉広葉樹林")
    return out
