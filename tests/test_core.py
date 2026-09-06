# -*- coding: utf-8 -*-
"""コアの動作確認。

QGIS の外で `python3 tests/test_core.py` として実行できる。
numpy 以外に依存しないので、CI にもそのまま載せられる。
"""

import collections
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))

import rinkyo_core as rc  # noqa: E402

# 常緑針葉樹（低NIR）と落葉広葉樹（高NIR）を含む、
# マニュアル図28の分布に似せた合成データ。
TRUTH = {
    "常緑針葉樹": ([350, 600, 400, 2500], 120),
    "落葉広葉樹": ([400, 750, 450, 6000], 200),
    "草地": ([500, 900, 700, 7000], 250),
    "水域": ([600, 500, 400, 250], 60),
    "裸地": ([1200, 1300, 1500, 2000], 300),
}
BAND_NAMES = ["Blue", "Green", "Red", "NIR", "NDVI"]
N_PER_CLASS = 40000


def require(condition, message):
    """assert は最適化時に消えるうえ Bandit B101 に引っかかるので使わない。"""
    if not condition:
        raise RuntimeError(message)


def make_data(seed=0):
    """4バンド + NDVI の合成データを作る。

    NDVI は Band4/Band8 の関数なので、この構成では共分散行列が
    悪条件になる。リッジ処理が効いているかの確認を兼ねている。
    """
    rng = np.random.default_rng(seed)
    blocks, truth = [], []
    for name, (mu, sd) in TRUTH.items():
        blocks.append(rng.normal(mu, sd, size=(N_PER_CLASS, 4)))
        truth += [name] * N_PER_CLASS
    x = np.vstack(blocks)
    ndvi = (x[:, 3] - x[:, 2]) / (x[:, 3] + x[:, 2])
    return np.column_stack([x, ndvi]), np.array(truth)


def main():
    x, truth = make_data()

    started = time.time()
    result = rc.cluster(x, n_classes=20, band_names=BAND_NAMES,
                        min_separation=0.35)
    elapsed = time.time() - started
    sig = result.signature
    print("クラスタリング: 反復%d回 収束%.1f%% クラス数%d "
          "(削除%d 統合%d) %.1f秒"
          % (result.n_iterations, result.convergence, sig.n_classes,
             result.dropped, result.merged, elapsed))

    print("\n各クラスの平均値:")
    for i in range(sig.n_classes):
        print("  %2d n=%7d %s NDVI=%.3f"
              % (i + 1, sig.counts[i],
                 " ".join("%8.1f" % v for v in sig.means[i][:4]),
                 sig.means[i][4]))

    started = time.time()
    cls, _ = rc.classify_array(x, sig)
    print("\n分類: %d画素 %.2f秒" % (len(cls), time.time() - started))

    purity = []
    for c in np.unique(cls):
        counter = collections.Counter(truth[cls == c])
        top, n = counter.most_common(1)[0]
        total = sum(counter.values())
        purity.append(n / total)
        print("  クラス%2d -> %s (純度%.3f n=%d)" % (c, top, n / total, total))
    mean_purity = float(np.mean(purity))
    print("平均純度 %.4f" % mean_purity)
    require(mean_purity > 0.85, "純度が低すぎます")

    print("\n自動ラベル案: %s" % rc.suggest_labels(sig, idx_ndvi=4))

    # 再現性: 同じ入力からは必ず同じシグネチャが出ること
    again = rc.cluster(x, n_classes=20, band_names=BAND_NAMES,
                       min_separation=0.35)
    require(np.array_equal(sig.means, again.signature.means),
            "同じ入力から違うシグネチャが出ました")
    print("再現性: OK")

    # 永続化の往復
    restored = rc.Signature.from_json(sig.to_json())
    require(np.allclose(restored.means, sig.means), "平均が復元できません")
    require(np.allclose(restored.covs, sig.covs), "共分散が復元できません")
    print("JSON往復: OK")

    print("\nすべて通りました。")


if __name__ == "__main__":
    main()
