"""管理員「記憶體明細」：常駐記憶體大概花在哪裡（10-19，找 Railway 記憶體費用的來源）。

只讀、不改任何資料；掃一次全部 Python 物件約數秒，只給管理員偶爾查。
最後試一次 malloc_trim：看「Python 已釋放、但還沒還給系統」的空間有多少。
"""
import ctypes
import gc
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

MB = 1024 * 1024


def rss_bytes() -> int:
    try:
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return pages * int(os.sysconf("SC_PAGE_SIZE"))
    except Exception:
        return 0


def approx_size(obj: Any, depth: int = 0, seen=None) -> int:
    """大略位元組數：資料表、陣列算實際內容；容器往下算 3 層；同一物件只算一次。"""
    seen = seen if seen is not None else set()
    if id(obj) in seen:
        return 0
    seen.add(id(obj))
    try:
        import numpy as np
        import pandas as pd
        if isinstance(obj, (pd.DataFrame, pd.Series)):
            return int(obj.memory_usage(deep=True).sum() if isinstance(obj, pd.DataFrame) else obj.memory_usage(deep=True))
        if isinstance(obj, np.ndarray):
            return int(obj.nbytes)
    except Exception:
        pass
    size = sys.getsizeof(obj, 0)
    if depth >= 3:
        return size
    if isinstance(obj, dict):
        size += sum(approx_size(k, depth + 1, seen) + approx_size(v, depth + 1, seen) for k, v in list(obj.items()))
    elif isinstance(obj, (list, tuple, set, frozenset)):
        size += sum(approx_size(v, depth + 1, seen) for v in list(obj))
    return size


def _cache_groups() -> List[Tuple[str, int, int]]:
    """工具快取（TTLCache）依 key 前綴分組：(前綴, 筆數, 大略位元組)。"""
    import warrant_ai_tools as tools
    cache = tools.CACHE
    with cache._lock:
        items = list(cache._data.items())
    groups: Dict[str, List[int]] = {}
    seen: set = set()
    for key, (_, value) in items:
        body = key.split("_", 3)[-1] if key.count("_") >= 3 else key
        prefix = body.split(":", 1)[0].split("|", 1)[0][:40]
        bucket = groups.setdefault(prefix, [0, 0])
        bucket[0] += 1
        bucket[1] += approx_size(value, 0, seen)
    return sorted(((k, v[0], v[1]) for k, v in groups.items()), key=lambda x: -x[2])


def _object_summary() -> Tuple[int, int, int, int, List[Tuple[str, int]]]:
    """(資料表數, 資料表總大小, 陣列數, 陣列總大小, 物件數最多的型別前 8)。"""
    import numpy as np
    import pandas as pd
    frames = frame_bytes = arrays = array_bytes = 0
    types: Counter = Counter()
    for obj in gc.get_objects():
        types[type(obj).__name__] += 1
        if isinstance(obj, pd.DataFrame):
            frames += 1
            try:
                frame_bytes += int(obj.memory_usage(deep=False).sum())
            except Exception:
                pass
        elif isinstance(obj, np.ndarray):
            arrays += 1
            array_bytes += int(obj.nbytes)
    return frames, frame_bytes, arrays, array_bytes, types.most_common(8)


def _malloc_trim() -> Tuple[int, int]:
    """呼叫 glibc malloc_trim(0)：把 Python 已釋放的記憶體還給系統；回傳 (前, 後) RSS。非 Linux 回 (0, 0)。"""
    before = rss_bytes()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        return 0, 0
    return before, rss_bytes()


def report(trim: bool = True) -> str:
    gc.collect()
    rss = rss_bytes()
    lines = ["**記憶體明細**", f"程式目前佔用（RSS）：{rss / MB:,.0f} MB"]
    try:
        groups = _cache_groups()
        total = sum(g[2] for g in groups)
        lines.append(f"工具快取：{sum(g[1] for g in groups):,} 筆｜約 {total / MB:,.0f} MB")
        for prefix, count, size in groups[:8]:
            lines.append(f"・{prefix}：{count:,} 筆｜約 {size / MB:,.1f} MB")
    except Exception as exc:
        lines.append(f"工具快取：無法統計（{type(exc).__name__}）")
    try:
        frames, frame_bytes, arrays, array_bytes, top = _object_summary()
        lines.append(f"所有資料表（DataFrame）：{frames:,} 個｜約 {frame_bytes / MB:,.0f} MB")
        lines.append(f"所有陣列（ndarray，和資料表有重疊）：{arrays:,} 個｜約 {array_bytes / MB:,.0f} MB")
        lines.append("物件數最多：" + "、".join(f"{name} {count:,}" for name, count in top))
    except Exception as exc:
        lines.append(f"物件統計：無法統計（{type(exc).__name__}）")
    try:
        import answer_image
        lines.append(f"字型快取：{answer_image.font.cache_info().currsize} 種字級")
    except Exception:
        pass
    if trim:
        before, after = _malloc_trim()
        if before:
            lines.append(f"釋放後歸還系統（malloc_trim）：{before / MB:,.0f} → {after / MB:,.0f} MB"
                         f"（還回 {max(0, before - after) / MB:,.0f} MB；數字大代表很多是「用完沒還」的空間）")
        else:
            lines.append("釋放後歸還系統：這台機器不支援（非 Linux），略過")
    return "\n".join(lines)
