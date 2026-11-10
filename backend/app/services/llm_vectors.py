"""向量打包：float32 序列化 / 反序列化 / L2 归一化。

这一组函数只做纯数值变换，不碰网络、不碰数据库，也不依赖 `llm.py`。
`pack_vector` 刻意不 import numpy（原因见函数注释），避免打包版首次加载
原生模块时占住 SQLite 写锁。
"""
from __future__ import annotations

import base64
import math
from typing import Iterable


def pack_vector(vec: list[float]) -> str:
    """把向量打包成 base64（float32 小端，与历史数据逐字节兼容）。

    为什么不用 numpy：这条函数在入库循环里逐块调用，而 numpy 是**懒加载**的。
    打包版首次 `import numpy` 要去加载 `_multiarray_umath.pyd` 和 OpenBLAS，
    Windows 上必须拿 loader lock —— 实测曾把 SQLite 写锁占了 155 秒，用户看到
    的就是「任务处理中整个软件卡住」（见 services/threadpool.py 的事故说明）。
    这里只需要把 float32 写进字节串，stdlib 的 array 就够了：
    同样 4 字节、同样本机小端，读回来的值与 numpy 版本完全一致。
    """
    import array

    arr = array.array("f", (float(v) for v in vec))
    return base64.b64encode(arr.tobytes()).decode("ascii")


def unpack_vector(blob: str | None) -> list[float] | None:
    """pack_vector 的逆操作；同样不依赖 numpy（原因见上）。"""
    if not blob:
        return None
    import array

    arr = array.array("f")
    arr.frombytes(base64.b64decode(blob.encode("ascii")))
    return arr.tolist()


def normalize(vec: Iterable[float]) -> list[float]:
    vals = [float(v) for v in vec]
    norm = math.sqrt(sum(v * v for v in vals)) or 1.0
    return [v / norm for v in vals]
