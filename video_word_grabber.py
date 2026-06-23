# -*- coding: utf-8 -*-
"""
视频字幕取词工具 (Video Subtitle Word Grabber)
================================================

启动后在屏幕上显示一个【可自由移动 / 缩放】的半透明取词框。把框的透明区域对准
视频字幕，每 0.5 秒自动截取框内画面 -> OCR 识别 -> 智能去重 -> 追加写入本地 txt。

核心特性
--------
* 透明 + 鼠标穿透的取词区：截图只抓取框后面的真实视频画面，绝不会把取词框自身
  的边框 / 控件喂给 OCR。
* DPI 缩放正确：在创建 Tk() 之前设置 Per-Monitor-V2 DPI 感知，使 tkinter 的坐标
  与 PIL.ImageGrab 的物理像素 1:1 对齐（解决 125% / 150% 缩放下截图错位的问题）。
* 多显示器 / 负坐标安全：使用 all_screens=True 抓取虚拟桌面；窗口被拖出屏幕或最
  小化（-32000 哨兵坐标）时跳过该次截图，避免 OCR 一片黑。
* 单工作线程 + 丢帧队列：OCR 在后台线程跑，GUI 不卡顿；OCR 慢于 0.5s 时自动丢弃
  积压的旧帧，永不堆积延迟。
* 智能流式去重（重点）：同一句字幕在连续多帧里、即使有细微 OCR 误差也只记录一次，
  并自动保留最完整 / 最干净的那一版；同时正确区分“真正不同的新字幕”。

智能去重为什么不是简单比较
--------------------------
连续两帧 OCR 出来的同一句话，可能因为渲染抖动 / 单字误识 / 全半角标点差异而文本
不同，但其实是同一句；反过来，"Chapter 1" 和 "Chapter 2" 字面很像却是两句不同的
话。本工具用一个单槽状态机 + 模糊相似度来处理（见 SubtitleDeduper 的注释）。

运行
----
    pip install -r requirements.txt
    python video_word_grabber.py

仅自测去重逻辑（不需要 GUI / OCR 依赖）：
    python video_word_grabber.py --selftest

作者备注：默认面向 Windows 11 + Python 3.10。
"""

from __future__ import annotations

import ctypes
import os
import math
import unicodedata
import sys
import queue
import threading
from collections import deque
from datetime import datetime
from difflib import SequenceMatcher
from typing import Deque, List, Optional, Tuple


# ===========================================================================
# 0. 可调常量
# ===========================================================================
INTERVAL_MS_DEFAULT = 500          # 默认取词间隔 0.5s
BORDER_THICKNESS = 2               # 取词区可见绿色边框厚度(物理像素)
INSET = BORDER_THICKNESS + 1       # 截图时向内收缩, 排除边框与抗锯齿光晕
TRANSPARENT_KEY = "#010203"        # 透明色键: 该颜色像素 -> 透明且鼠标穿透
BORDER_COLOR = "#00E000"           # 取词区可见边框颜色(不能等于色键)
BAR_BG = "#202830"                 # 顶/底不透明操作条背景
BAR_FG = "#E6E6E6"
TOP_BAR_H = 26
BOTTOM_BAR_H = 20

DEFAULT_OUTPUT_NAME = "取词结果.txt"


# ===========================================================================
# 1. 相似度后端 (rapidfuzz 可选加速, 否则纯标准库 difflib)
# ===========================================================================
try:  # pragma: no cover - 取决于环境
    from rapidfuzz.fuzz import ratio as _rf_ratio

    def _similarity(a: str, b: str) -> float:
        if not a and not b:
            return 1.0
        return _rf_ratio(a, b) / 100.0
except Exception:  # pragma: no cover

    def _similarity(a: str, b: str) -> float:
        if not a and not b:
            return 1.0
        return SequenceMatcher(None, a, b).ratio()


# CJK / 日文假名等表意文字区间: 用于判断“单字也是一句完整字幕”而非 OCR 噪点。
_CJK_RANGES = (
    (0x4E00, 0x9FFF),   # CJK 统一表意文字
    (0x3400, 0x4DBF),   # CJK 扩展 A
    (0xF900, 0xFAFF),   # CJK 兼容表意
    (0x3040, 0x30FF),   # 平假名 + 片假名
    (0xAC00, 0xD7A3),   # 韩文音节
)


def _has_cjk(s: str) -> bool:
    for ch in s:
        o = ord(ch)
        for lo, hi in _CJK_RANGES:
            if lo <= o <= hi:
                return True
    return False


# ===========================================================================
# 2. 智能流式去重器
# ===========================================================================
def _bounded_lev(a: str, b: str, max_dist: int) -> int:
    """Levenshtein distance with a diagonal band + early exit.
    Returns the true distance if <= max_dist, else max_dist+1."""
    la, lb = len(a), len(b)
    if abs(la - lb) > max_dist:
        return max_dist + 1
    INF = max_dist + 1
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        lo = max(1, i - max_dist)
        hi = min(lb, i + max_dist)
        if lo > 1:
            cur[lo - 1] = INF
        row_min = cur[0]
        for j in range(lo, hi + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if cur[j] < row_min:
                row_min = cur[j]
        if hi < lb:
            cur[hi + 1] = INF
        if row_min > max_dist:
            return INF
        prev = cur
    return prev[lb] if prev[lb] <= max_dist else INF


class SubtitleDeduper:
    """Streaming deduplicator for OCR'd subtitle captures.

    Public interface (unchanged):
        dd = SubtitleDeduper(stream_mode="grow")
        line = dd.feed(ocr_text)   # ~ every 0.5s, returns Optional[str]
        tail = dd.flush()          # at shutdown, returns Optional[str]
        cur  = dd.peek_current()   # Optional[str]
    """

    def __init__(
        self,
        *,
        stream_mode: str = "grow",
        same_threshold: float = 0.78,
        commit_threshold: float = 0.78,
        empty_gap_frames: int = 2,
        recent_window: int = 12,
        flicker_window: int = 1,
        min_chars: int = 1,
        switch_min_chars: int = 1,
        # --- noise-tolerant growth predicate tunables ---
        noise_rate: float = 0.25,
        noise_floor: int = 1,
        equal_len_is_growth: bool = False,
        head_anchor_chars: int = 1,
        # --- scroll-stitch tunables ---
        min_overlap: int = 2,
        max_overlap: int = 24,
        max_buffer_chars: int = 400,
        grow_sim: float = 0.70,
    ) -> None:
        if stream_mode not in ("grow", "scroll", "line"):
            raise ValueError("stream_mode must be one of grow/scroll/line")
        self.stream_mode = stream_mode
        self.same_threshold = same_threshold
        self.commit_threshold = commit_threshold
        self.empty_gap_frames = empty_gap_frames
        self.recent_window = recent_window
        self.flicker_window = flicker_window
        self.min_chars = min_chars
        self.switch_min_chars = switch_min_chars

        self.noise_rate = noise_rate
        self.noise_floor = noise_floor
        self.equal_len_is_growth = equal_len_is_growth
        self.head_anchor_chars = head_anchor_chars

        self.min_overlap = min_overlap
        self.max_overlap = max_overlap
        self.max_buffer_chars = max_buffer_chars
        self.grow_sim = grow_sim

        # Current-subtitle state (single slot).
        self._cur_raw: Optional[str] = None
        self._cur_norm: str = ""
        self._cur_score: float = float("-inf")
        self._empty_run: int = 0

        # Rolling history of recently committed normalized lines (flicker guard).
        self._recent: Deque[str] = deque(maxlen=recent_window)

    # ------------------------------------------------------------------ public

    def feed(self, text: Optional[str]) -> Optional[str]:
        raw = text or ""
        norm = self._normalize(raw)

        # --- empty / blank capture -------------------------------------------
        if len(norm) < self.min_chars:
            if self._cur_raw is None:
                return None
            self._empty_run += 1
            if self._empty_run >= self.empty_gap_frames:
                return self._finalize()
            return None

        self._empty_run = 0

        # --- first subtitle ever ---------------------------------------------
        if self._cur_raw is None:
            if self._matches_recent(norm):
                return None
            self._start(raw, norm)
            return None

        # --- exact / flicker fast path ---------------------------------------
        if norm == self._cur_norm:
            self._maybe_replace_best(raw, norm)
            return None

        # --- mode-specific same-line / growth / scroll decision --------------
        if self.stream_mode == "scroll":
            handled, out = self._feed_scroll(raw, norm)
            if handled:
                return out
        else:
            # grow + line both use the growth predicate; "line" uses a tighter
            # gate so whole-line replacements finalize quickly.
            if self._is_same_line(raw, norm):
                self._merge_same(raw, norm)
                return None

        # --- different capture -----------------------------------------------
        if len(norm) < self.switch_min_chars:
            return None

        if self._matches_recent(norm):
            return self._finalize()

        out = self._finalize()
        self._start(raw, norm)
        return out

    def flush(self) -> Optional[str]:
        if self._cur_raw is None:
            return None
        return self._finalize()

    def peek_current(self) -> Optional[str]:
        return self._cur_raw

    # --------------------------------------------------- same-line / growth

    def _is_same_line(self, raw: str, norm: str) -> bool:
        """Whether `norm` is the same on-screen line as the tracked current line.

        Combines: fuzzy whole-string similarity (noisy repeat of equal-ish line)
        and the noise-tolerant growth predicate (progressive reveal with a misread
        or dropped char). In "line" mode growth is disabled, only high similarity
        of an equal-ish whole line counts.
        """
        if self.stream_mode == "line":
            # Whole-line: only treat as same line if very close in content AND
            # equal length (a length growth means a changing line). This keeps
            # Chapter 1 / Chapter 2 (equal length, last char differs, ~0.88 sim)
            # SEPARATE because those are finalized across a blank gap and start
            # fresh against an empty slot anyway -- and back-to-back distinct
            # whole lines (Hello/World, low sim) never merge.
            return _similarity(norm, self._cur_norm) >= self.same_threshold and \
                len(norm) == len(self._cur_norm)

        # grow mode:
        if self._is_extension(self._cur_norm, norm):
            return True
        # A noisy frame may momentarily neither grow nor exact-prefix-match (it
        # both drops and reveals a char, e.g. 我们一起 -> 我一起去: equal length,
        # head shared, content drifting). Treat as the same drifting line when it
        # shares the head anchor AND has substantial contiguous overlap with the
        # current line. The head-anchor requirement keeps an unrelated short line
        # (different first char) from merging; the overlap floor (grow_sim) keeps
        # two truly different lines apart (e.g. 今天我要 / 今天他来 == 0.5 < 0.70).
        # (Chapter1/Chapter2-style numbered lines are whole-line subtitles handled
        # by "line" mode, not grow mode.)
        if norm[:1] == self._cur_norm[:1] and \
                _similarity(norm, self._cur_norm) >= self.grow_sim:
            return True
        return False

    def _merge_same(self, raw: str, norm: str) -> None:
        self._maybe_replace_best(raw, norm)

    def _budget(self, n: int) -> int:
        return math.ceil(n * self.noise_rate) + self.noise_floor

    def _approx_prefix_dist(self, short: str, longv: str, budget: int) -> int:
        k = len(short)
        best = budget + 1
        lo = max(0, k - budget)
        hi = min(len(longv), k + budget)
        for plen in range(lo, hi + 1):
            d = _bounded_lev(short, longv[:plen], budget)
            if d < best:
                best = d
            if best == 0:
                break
        return best

    def _is_extension(self, cur_norm: str, new_norm: str) -> bool:
        """True if new_norm is plausibly the SAME growing line as cur_norm
        (growth or a slightly-shorter/noisier view), tolerant to a misread or
        dropped char in the overlapping prefix region. Direction-agnostic."""
        if not cur_norm or not new_norm:
            return False

        # 1) exact prefix in either direction (clean typewriter / repeat)
        if new_norm.startswith(cur_norm) or cur_norm.startswith(new_norm):
            return True

        short, longv = ((cur_norm, new_norm)
                        if len(cur_norm) <= len(new_norm)
                        else (new_norm, cur_norm))
        ls, ll = len(short), len(longv)

        # 2) growth requires a length change. Equal-length differing strings are
        #    the Chapter1/Chapter2 trap and are owned by flicker/distinct logic.
        if ls == ll and not self.equal_len_is_growth:
            return False

        # 3) head anchor: first char(s) must match exactly. Rejects left-sliding
        #    windows (今天天气很 vs 天气很好我们) and unrelated short CJK (好 vs 坏吗).
        hac = self.head_anchor_chars
        if short[:hac] != longv[:hac]:
            return False

        # 4) short must be an approximate prefix of long within the noise budget.
        #    Absorbs the single misread/dropped char (速度 ~ prefix 速喥 of 速喥很).
        budget = self._budget(ls)
        return self._approx_prefix_dist(short, longv, budget) <= budget

    # --------------------------------------------------------- scroll stitching

    def _scroll_overlap(self, buf: str, new: str) -> int:
        """Largest k with buf[-k:] == new[:k], k < len(buf) (head scrolls off).
        EXACT anchor only -- no fuzz -- so two distinct lines sharing a long
        common substring (Chapter 1 / Chapter 2) never false-stitch."""
        max_k = min(len(buf) - 1, len(new), self.max_overlap)
        for k in range(max_k, self.min_overlap - 1, -1):
            if buf[-k:] == new[:k]:
                return k
        return 0

    def _feed_scroll(self, raw: str, norm: str) -> Tuple[bool, Optional[str]]:
        """Returns (handled, emitted). handled=True means the capture was fully
        consumed by the scroll logic (caller should return emitted)."""
        buf = self._cur_norm

        # 1) contained / pure repeat
        if norm in buf:
            if norm == buf:
                self._maybe_replace_best(raw, norm)
            return True, None

        # 2) clean full-prefix growth -> adopt the longer frame wholesale
        #    (typewriter & whole-line ride this branch, never over-stitch).
        if norm.startswith(buf):
            self._adopt(raw, norm)
            return True, None

        # 3) fixed-left-edge growth despite a midword misread (BAD#2), via the
        #    noise-tolerant growth predicate.
        if self._is_extension(buf, norm):
            if len(norm) >= len(buf):
                self._adopt(raw, norm)
            return True, None

        # 4) genuine LEFT SCROLL: head scrolled off, tail == new's head. Append
        #    only the non-overlapping RAW tail so emitted text keeps punctuation.
        k = self._scroll_overlap(buf, norm)
        if k:
            drop = self._raw_prefix_len_for_norm(raw, k)
            new_raw = (self._cur_raw or "") + raw[drop:]
            new_norm = self._normalize(new_raw)
            if len(new_norm) > self.max_buffer_chars:   # runaway guard
                new_raw = new_raw[-self.max_buffer_chars:]
                new_norm = self._normalize(new_raw)
            self._cur_raw = new_raw
            self._cur_norm = new_norm
            self._cur_score = self._quality(new_raw, new_norm)
            return True, None

        # 5) no relationship -> distinct line. Not handled here; let the generic
        #    finalize+start path run (with flicker guard) so the buffer is emitted
        #    and a fresh one starts.
        return False, None

    def _adopt(self, raw: str, norm: str) -> None:
        """Adopt a longer/cleaner frame wholesale (scroll grow path)."""
        self._cur_raw = raw.strip()
        self._cur_norm = norm
        self._cur_score = self._quality(raw, norm)

    def _raw_prefix_len_for_norm(self, raw: str, k_norm: int) -> int:
        for i in range(1, len(raw) + 1):
            if len(self._normalize(raw[:i])) >= k_norm:
                return i
        return len(raw)

    # ----------------------------------------------------------------- helpers

    def _start(self, raw: str, norm: str) -> None:
        self._cur_raw = raw.strip()
        self._cur_norm = norm
        self._cur_score = self._quality(raw, norm)
        self._empty_run = 0

    def _maybe_replace_best(self, raw: str, norm: str) -> None:
        score = self._quality(raw, norm)
        if score > self._cur_score:
            self._cur_raw = raw.strip()
            self._cur_norm = norm
            self._cur_score = score

    def _finalize(self) -> Optional[str]:
        out = self._cur_raw
        if out is not None and self._cur_norm:
            self._recent.append(self._cur_norm)
        self._cur_raw = None
        self._cur_norm = ""
        self._cur_score = float("-inf")
        self._empty_run = 0
        return out

    def _matches_recent(self, norm: str) -> bool:
        """Time-bounded flicker suppression: a line that just left the screen and
        immediately re-appears (within the last ``flicker_window`` commits) is the
        same flickering line, not a new occurrence. A LEGITIMATE repeat far apart
        (several intervening committed lines) falls outside this window and is
        emitted again. Requires near-identity (exact, >=0.97) AND equal length so
        a distinct line sharing a stem is never suppressed."""
        if self.flicker_window <= 0:
            return False
        # only the most recent commit(s) count as "flicker adjacent"
        recent = list(self._recent)[-self.flicker_window:]
        for prev in recent:
            if len(norm) == len(prev) and _similarity(norm, prev) >= 0.97:
                return True
        return False

    # ------------------------------------------------------- quality + normalize

    @staticmethod
    def _quality(raw: str, norm: str) -> float:
        """Heuristic completeness/cleanliness score: normalized length dominates
        (longer == more complete OCR), minus a small messy-whitespace penalty,
        plus a bonus for sentence-final punctuation (a complete line)."""
        if not norm:
            return float("-inf")
        n = len(norm)
        stripped = raw.strip()
        collapsed = " ".join(stripped.split())
        extra_ws = (len(stripped) - len(collapsed))
        complete_bonus = 0.5 if stripped[-1:] in ".!?。！？…" else 0.0
        return n - extra_ws * 0.3 + complete_bonus

    @classmethod
    def _normalize(cls, text: str) -> str:
        """Comparison key only: NFKC width-fold, lowercase, strip whitespace and
        a transient-punctuation set. NO char-confusion folding. Never stored."""
        if not text:
            return ""
        t = unicodedata.normalize("NFKC", text).lower()
        t = "".join(t.split())
        drop = set(".,!?;:'\"`~^*_-=+()[]{}<>/\\|…。，！？；：、‘’“”《》（）")
        return "".join(ch for ch in t if ch not in drop)


# ===========================================================================
# 3. OCR 引擎封装 (RapidOCR / rapidocr-onnxruntime)
# ===========================================================================
# 注意: 必须在 import onnxruntime 之前限制线程数。多核 CPU(如 32 逻辑核)默认会把
# 一张极小的字幕图铺到所有核上, 线程同步开销反而让单次推理慢几十倍。
os.environ.setdefault("OMP_NUM_THREADS", "6")
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")

_ocr_engine = None
_ocr_lock = threading.Lock()   # onnxruntime 会话非线程安全, 串行化调用


def init_ocr():
    """创建 OCR 引擎(幂等)。首次较重(~0.25s 加载模型), 在工作线程启动时调用。"""
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR(
            intra_op_num_threads=6,
            use_angle_cls=False,      # 字幕不旋转, 跳过方向分类, 省一个模型
            det_limit_type="max",     # 关键: 限制长边, 别把矮字幕条放大 8 倍
            det_limit_side_len=960,
            text_score=0.5,
        )
    return _ocr_engine


def _is_cjk_char(ch: str) -> bool:
    return _has_cjk(ch)


def ocr_image(pil_image) -> str:
    """对裁剪好的字幕区域图做 OCR, 返回识别文本(多框按版面排序后拼接)。"""
    import numpy as np
    eng = _ocr_engine or init_ocr()

    if pil_image.mode != "RGB":
        pil_image = pil_image.convert("RGB")
    arr = np.asarray(pil_image)

    with _ocr_lock:
        result, _elapse = eng(arr, use_cls=False)

    if not result:
        return ""

    boxes = []  # (top, left, bottom, text)
    for box, text, _score in result:
        text = (text or "").strip()
        if not text:
            continue
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        boxes.append((min(ys), min(xs), max(ys), text))
    if not boxes:
        return ""

    boxes.sort(key=lambda b: (b[0], b[1]))

    # 把竖直方向重叠的框归为同一视觉行
    lines, cur = [], [boxes[0]]
    for b in boxes[1:]:
        top, _x, _bot, _t = b
        cur_top = min(c[0] for c in cur)
        cur_bot = max(c[2] for c in cur)
        h = max(1.0, cur_bot - cur_top)
        if top < cur_bot - 0.5 * h:
            cur.append(b)
        else:
            lines.append(cur)
            cur = [b]
    lines.append(cur)

    out_lines = []
    for ln in lines:
        ln.sort(key=lambda b: b[1])
        parts = [b[3] for b in ln]
        joined = ""
        for i, p in enumerate(parts):
            if i == 0:
                joined = p
            else:
                prev_cjk = _is_cjk_char(joined[-1])
                cur_cjk = _is_cjk_char(p[0])
                joined += ("" if (prev_cjk and cur_cjk) else " ") + p
        out_lines.append(joined.strip())

    return "\n".join(l for l in out_lines if l)


# ===========================================================================
# 4. DPI 感知 (必须在 Tk() 之前调用, 且检查返回值)
# ===========================================================================
def enable_dpi_awareness() -> bool:
    """让 tkinter 的 winfo_* 逻辑坐标等于物理像素, 与 PIL.ImageGrab 1:1 对齐。

    依次尝试 Per-Monitor-V2 -> Per-Monitor -> System; 并**检查返回值**:
    这些 API 失败时只返回错误码而不抛异常, 仅靠 try/except 会静默退化成 DPI 不感知。
    """
    if sys.platform != "win32":
        return False
    # Per-Monitor-V2 (Win10 1703+): SetProcessDpiAwarenessContext 返回 BOOL, 0=失败
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return True
    except (AttributeError, OSError):
        pass
    # Per-Monitor (Win8.1+): SetProcessDpiAwareness 返回 HRESULT
    #   S_OK(0) = 成功; E_ACCESSDENIED(0x80070005) = 已被设置过, 也算成功
    try:
        hr = ctypes.windll.shcore.SetProcessDpiAwareness(2)
        if hr == 0 or hr == -2147024891:
            return True
    except (AttributeError, OSError):
        pass
    # System DPI aware (Vista+): 返回 BOOL
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return True
    except (AttributeError, OSError):
        pass
    return False


def virtual_screen_rect() -> Optional[Tuple[int, int, int, int]]:
    """返回虚拟桌面矩形 (x0, y0, x1, y1)(物理像素), 失败返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        gsm = ctypes.windll.user32.GetSystemMetrics
        x = gsm(76)   # SM_XVIRTUALSCREEN
        y = gsm(77)   # SM_YVIRTUALSCREEN
        w = gsm(78)   # SM_CXVIRTUALSCREEN
        h = gsm(79)   # SM_CYVIRTUALSCREEN
        if w <= 0 or h <= 0:
            return None
        return (x, y, x + w, y + h)
    except (AttributeError, OSError):
        return None


# ===========================================================================
# 5. 透明可缩放取词框 (Toplevel)
# ===========================================================================
def _import_tk():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    return tk, ttk, filedialog, messagebox


class CaptureOverlay:
    """顶部不透明操作条 + 中间透明取词区 + 底部不透明操作条(含缩放手柄)。

    截图只抓“中间透明区”, 顶/底操作条都在取词区之外, 因此 OCR 永远拿不到工具自身
    的任何像素。中间区背景使用透明色键 -> 真正透明且鼠标穿透(可继续操作下方播放器)。
    """

    def __init__(self, master, tkmod):
        tk = tkmod
        self.tk = tk
        self.win = tk.Toplevel(master)
        win = self.win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=TRANSPARENT_KEY)
        if sys.platform == "win32":
            win.wm_attributes("-transparentcolor", TRANSPARENT_KEY)
        win.geometry("720x150+300+640")

        # 顶部操作条(移动)
        self.top_bar = tk.Frame(win, bg=BAR_BG, height=TOP_BAR_H)
        self.top_bar.pack(side="top", fill="x")
        self.top_bar.pack_propagate(False)
        self.title_lbl = tk.Label(
            self.top_bar, bg=BAR_BG, fg=BAR_FG,
            text="字幕取词框 — 拖此条移动 / 右下角缩放，点右侧按钮启动 ▶",
            anchor="w", padx=8, font=("Microsoft YaHei UI", 9),
        )
        self.title_lbl.pack(side="left", fill="both", expand=True)
        # 直接在取词框上提供开始/停止, 不必去找控制面板
        self.on_toggle = None
        self.toggle_btn = tk.Button(
            self.top_bar, text="▶ 开始", command=self._on_toggle_click,
            bg="#2e7d32", fg="white", activebackground="#388e3c", activeforeground="white",
            relief="flat", bd=0, padx=10, cursor="hand2", font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.toggle_btn.pack(side="right", padx=4, pady=3)
        self.live_lbl = tk.Label(self.top_bar, bg=BAR_BG, fg="#9fe09f",
                                 text="待机", padx=8, font=("Microsoft YaHei UI", 9))
        self.live_lbl.pack(side="right")

        # 中间取词区(透明 + 绿色描边)
        self.capture_area = tk.Frame(
            win, bg=TRANSPARENT_KEY,
            highlightthickness=BORDER_THICKNESS,
            highlightbackground=BORDER_COLOR,
            highlightcolor=BORDER_COLOR,
        )
        self.capture_area.pack(side="top", fill="both", expand=True)

        # 底部操作条(含缩放手柄)
        self.bottom_bar = tk.Frame(win, bg=BAR_BG, height=BOTTOM_BAR_H)
        self.bottom_bar.pack(side="bottom", fill="x")
        self.bottom_bar.pack_propagate(False)
        self.hint_lbl = tk.Label(self.bottom_bar, bg=BAR_BG, fg="#7a8a99",
                                 text="把上方绿框对准字幕", padx=8,
                                 font=("Microsoft YaHei UI", 8))
        self.hint_lbl.pack(side="left")
        self.grip = tk.Label(self.bottom_bar, bg=BORDER_COLOR, text="◢",
                             fg="#103010", cursor="sizing", width=2)
        self.grip.pack(side="right")

        # 绑定: 移动(顶/底条) + 缩放(手柄)
        for w in (self.top_bar, self.title_lbl, self.hint_lbl):
            w.bind("<ButtonPress-1>", self._press_move)
            w.bind("<B1-Motion>", self._drag)
        self.grip.bind("<ButtonPress-1>", self._press_resize)
        self.grip.bind("<B1-Motion>", self._drag)

        self._mode = None

    # --- 拖动 / 缩放 -------------------------------------------------------
    def _press_move(self, e):
        self._mode = "move"
        self._ox, self._oy = e.x_root, e.y_root
        self._wx, self._wy = self.win.winfo_rootx(), self.win.winfo_rooty()

    def _press_resize(self, e):
        self._mode = "resize"
        self._ox, self._oy = e.x_root, e.y_root
        self._ow, self._oh = self.win.winfo_width(), self.win.winfo_height()
        return "break"

    def _drag(self, e):
        dx, dy = e.x_root - self._ox, e.y_root - self._oy
        if self._mode == "move":
            self.win.geometry(f"+{self._wx + dx}+{self._wy + dy}")
        elif self._mode == "resize":
            w = max(120, self._ow + dx)
            h = max(TOP_BAR_H + BOTTOM_BAR_H + 30, self._oh + dy)
            self.win.geometry(f"{w}x{h}")

    # --- 取词区屏幕坐标 ----------------------------------------------------
    def interior_bbox(self) -> Optional[Tuple[int, int, int, int]]:
        self.win.update_idletasks()
        ca = self.capture_area
        x = ca.winfo_rootx() + INSET
        y = ca.winfo_rooty() + INSET
        w = ca.winfo_width() - 2 * INSET
        h = ca.winfo_height() - 2 * INSET
        if w < 1 or h < 1:
            return None
        bbox = (x, y, x + w, y + h)
        # 窗口被最小化 / 拖出屏幕时 rootx/rooty 会是 -32000 之类的哨兵值
        if x <= -30000 or y <= -30000:
            return None
        vrect = virtual_screen_rect()
        if vrect is not None:
            vx0, vy0, vx1, vy1 = vrect
            # 取词区中心必须落在虚拟桌面内, 否则抓到的多半是黑屏
            cx, cy = x + w // 2, y + h // 2
            if not (vx0 <= cx < vx1 and vy0 <= cy < vy1):
                return None
        return bbox

    def grab_interior(self):
        from PIL import ImageGrab
        bbox = self.interior_bbox()
        if bbox is None:
            return None
        try:
            return ImageGrab.grab(bbox=bbox, all_screens=True)
        except Exception:
            return None

    def set_live(self, text: str, color: str = "#9fe09f"):
        try:
            self.live_lbl.configure(text=text, fg=color)
        except Exception:
            pass

    def _on_toggle_click(self):
        if callable(self.on_toggle):
            self.on_toggle()

    def set_running(self, running: bool):
        """同步取词框顶栏按钮与状态指示。"""
        try:
            if running:
                self.toggle_btn.configure(text="■ 停止", bg="#c62828", activebackground="#d32f2f")
                self.set_live("● 取词中", "#ff8a8a")
            else:
                self.toggle_btn.configure(text="▶ 开始", bg="#2e7d32", activebackground="#388e3c")
                self.set_live("待机")
        except Exception:
            pass


# ===========================================================================
# 6. 主控制面板 + 采集/识别管线
# ===========================================================================
class App:
    def __init__(self):
        tk, ttk, filedialog, messagebox = _import_tk()
        self.tk, self.ttk = tk, ttk
        self.filedialog, self.messagebox = filedialog, messagebox

        self.root = tk.Tk()
        self.root.title("视频字幕取词工具")
        self.root.geometry("440x520+80+80")
        self.root.minsize(380, 420)
        self.root.attributes("-topmost", True)   # 置顶, 避免被全屏视频盖住而找不到

        self.overlay = CaptureOverlay(self.root, tk)
        self.overlay.on_toggle = self.toggle     # 取词框顶栏按钮也能开始/停止

        # 运行状态
        self._running = False
        self._cap_job = None
        self._count = 0
        self._frame_q: "queue.Queue" = queue.Queue()
        self._ui_q: "queue.Queue" = queue.Queue()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._dedup: Optional[SubtitleDeduper] = None

        default_dir = os.path.dirname(os.path.abspath(__file__))
        self.output_path = tk.StringVar(value=os.path.join(default_dir, DEFAULT_OUTPUT_NAME))
        self.interval_s = tk.DoubleVar(value=INTERVAL_MS_DEFAULT / 1000.0)
        self.similarity = tk.DoubleVar(value=0.78)
        self.with_ts = tk.BooleanVar(value=False)
        # 字幕出现方式 -> 去重模式
        self._mode_map = {
            "逐字增长(打字机)": "grow",
            "滚动条/滑动窗口": "scroll",
            "整句一次性出现": "line",
        }
        self.mode_display = tk.StringVar(value="逐字增长(打字机)")
        self.status_var = tk.StringVar(value="待机：请先把绿框对准字幕，再点“开始取词”")
        self.count_var = tk.StringVar(value="已记录 0 句")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(150, self._poll_ui)

    # --------------------------------------------------------------- UI 布局
    def _build_ui(self):
        tk, ttk = self.tk, self.ttk
        pad = {"padx": 10, "pady": 4}

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="输出文件:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.output_path, width=34).grid(row=1, column=0, sticky="we")
        ttk.Button(top, text="浏览…", command=self._choose_file).grid(row=1, column=1, padx=(6, 0))
        top.columnconfigure(0, weight=1)

        opt = ttk.LabelFrame(self.root, text="参数")
        opt.pack(fill="x", **pad)
        ttk.Label(opt, text="字幕类型:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Combobox(opt, textvariable=self.mode_display, state="readonly", width=18,
                     values=list(self._mode_map.keys())
                     ).grid(row=0, column=1, columnspan=3, sticky="w", pady=4)
        ttk.Label(opt, text="取词间隔(秒):").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        ttk.Spinbox(opt, from_=0.2, to=5.0, increment=0.1, width=6,
                    textvariable=self.interval_s).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(opt, text="合并阈值:").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        self._sim_lbl = ttk.Label(opt, text="0.78")
        ttk.Scale(opt, from_=0.55, to=0.95, variable=self.similarity, orient="horizontal",
                  command=lambda v: self._sim_lbl.configure(text=f"{float(v):.2f}")
                  ).grid(row=2, column=1, columnspan=2, sticky="we", pady=4)
        self._sim_lbl.grid(row=2, column=3, padx=6)
        ttk.Checkbutton(opt, text="每句前加时间戳", variable=self.with_ts
                        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=6, pady=4)
        opt.columnconfigure(2, weight=1)

        btns = ttk.Frame(self.root)
        btns.pack(fill="x", **pad)
        self.btn_start = ttk.Button(btns, text="▶ 开始取词", command=self.start)
        self.btn_stop = ttk.Button(btns, text="■ 停止", command=self.stop, state="disabled")
        self.btn_test = ttk.Button(btns, text="测试一次", command=self.test_once)
        self.btn_start.pack(side="left")
        self.btn_stop.pack(side="left", padx=6)
        self.btn_test.pack(side="left")

        ttk.Label(self.root, textvariable=self.count_var,
                  font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=10)

        lf = ttk.LabelFrame(self.root, text="已记录(去重后)")
        lf.pack(fill="both", expand=True, **pad)
        self.listbox = tk.Listbox(lf, activestyle="none")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        ttk.Label(self.root, textvariable=self.status_var, wraplength=420,
                  foreground="#555").pack(fill="x", padx=10, pady=(0, 8))

    def _choose_file(self):
        path = self.filedialog.asksaveasfilename(
            title="选择输出 txt 文件", defaultextension=".txt",
            initialfile=DEFAULT_OUTPUT_NAME, filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if path:
            self.output_path.set(path)

    # --------------------------------------------------------- 启停 + 采集
    def toggle(self):
        """供取词框顶栏按钮调用: 在“开始/停止”之间切换。"""
        if self._running:
            self.stop()
        else:
            self.start()

    def start(self):
        if self._running:
            return
        try:
            interval_ms = max(100, int(float(self.interval_s.get()) * 1000))
        except Exception:
            interval_ms = INTERVAL_MS_DEFAULT
        self._interval_ms = interval_ms

        # 每次开始都用当前阈值/模式重建去重器(状态归零)
        mode = self._mode_map.get(self.mode_display.get(), "grow")
        self._dedup = SubtitleDeduper(stream_mode=mode,
                                      same_threshold=float(self.similarity.get()))

        # 清空积压帧
        self._drain_queue(self._frame_q)
        self._stop_event.clear()

        self._worker = threading.Thread(target=self._worker_loop,
                                        args=(self.output_path.get(), bool(self.with_ts.get())),
                                        daemon=True)
        self._worker.start()

        self._running = True
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_test.configure(state="disabled")
        self.overlay.set_running(True)
        self.status_var.set("正在加载 OCR 模型（首次约需几秒）…")
        self._capture_tick()

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._cap_job is not None:
            self.root.after_cancel(self._cap_job)
            self._cap_job = None
        # 通知工作线程收尾(它会 flush 最后一句)
        self._stop_event.set()
        self._frame_q.put(None)  # 哨兵
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.btn_test.configure(state="normal")
        self.overlay.set_running(False)
        self.status_var.set("正在停止并保存最后一句…")

    def _capture_tick(self):
        if self._running:
            img = self.overlay.grab_interior()
            if img is not None:
                self._enqueue_frame(img)
            self._cap_job = self.root.after(self._interval_ms, self._capture_tick)

    def _enqueue_frame(self, img):
        # 丢帧: 只保留最新一帧, OCR 慢也不会堆积延迟
        self._drain_queue(self._frame_q)
        self._frame_q.put(img)

    @staticmethod
    def _drain_queue(q: "queue.Queue"):
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass

    # ----------------------------------------------------------- 工作线程
    def _worker_loop(self, out_path: str, with_ts: bool):
        # 惰性初始化 OCR(放在工作线程, 不阻塞 GUI 启动)
        try:
            self._ui_put(("status", "正在加载 OCR 模型…"))
            init_ocr()
        except Exception as e:  # 依赖缺失 / 模型加载失败
            self._ui_put(("error",
                          "OCR 初始化失败：%s\n\n请先安装依赖：\n"
                          "pip install -r requirements.txt\n"
                          "（核心：pip install rapidocr-onnxruntime pillow numpy）" % e))
            return

        fh = None
        try:
            fh = open(out_path, "a", encoding="utf-8")
            fh.write("\n# ==== 取词会话开始 %s ====\n"
                     % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            fh.flush()
        except Exception as e:
            self._ui_put(("error", "无法写入输出文件：%s\n%s" % (out_path, e)))
            return

        def write_line(line: str):
            ts = ("[%s] " % datetime.now().strftime("%H:%M:%S")) if with_ts else ""
            fh.write(ts + line + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())   # 断电也尽量不丢已记录的行
            except OSError:
                pass

        self._ui_put(("status", "运行中：每 %.1fs 取词一次。" % (self._interval_ms / 1000.0)))
        try:
            while True:
                try:
                    item = self._frame_q.get(timeout=0.25)
                except queue.Empty:
                    if self._stop_event.is_set():
                        break
                    continue
                if item is None:        # 停止哨兵
                    break
                try:
                    text = ocr_image(item)
                except Exception as e:
                    self._ui_put(("status", "OCR 错误：%s" % e))
                    continue
                line = self._dedup.feed(text)
                if line:
                    write_line(line)
                    self._ui_put(("line", line))
            # 收尾: 提交正在跟踪中的最后一句
            tail = self._dedup.flush()
            if tail:
                write_line(tail)
                self._ui_put(("line", tail))
        finally:
            try:
                fh.close()
            except Exception:
                pass
        self._ui_put(("status", "已停止。结果已保存到：%s" % out_path))

    def _ui_put(self, msg):
        self._ui_q.put(msg)

    # ------------------------------------------------------------- UI 轮询
    def _poll_ui(self):
        try:
            while True:
                kind, payload = self._ui_q.get_nowait()
                if kind == "line":
                    self._count += 1
                    self.listbox.insert("end", payload)
                    self.listbox.see("end")
                    self.count_var.set("已记录 %d 句" % self._count)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "test":
                    self.btn_test.configure(state="normal" if not self._running else "disabled")
                    self.messagebox.showinfo("测试一次 OCR 结果",
                                             payload if payload else "（未识别到文字）")
                elif kind == "error":
                    self.status_var.set("错误")
                    self.overlay.set_live("错误", "#ff6060")
                    self.messagebox.showerror("错误", payload)
        except queue.Empty:
            pass
        self.root.after(150, self._poll_ui)

    # ------------------------------------------------------------- 测试一次
    def test_once(self):
        img = self.overlay.grab_interior()
        if img is None:
            self.messagebox.showwarning("提示", "取词区无效（窗口太小或被拖出屏幕）。")
            return
        self.btn_test.configure(state="disabled")
        self.status_var.set("测试识别中…")

        def run():
            try:
                init_ocr()
                text = ocr_image(img)
            except Exception as e:
                self._ui_put(("error", "测试失败：%s" % e))
                return
            self._ui_put(("test", text))

        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------- 关闭
    def _on_close(self):
        if self._running:
            self.stop()
        # 给工作线程一点时间 flush 落盘
        w = self._worker
        if w is not None and w.is_alive():
            w.join(timeout=3.0)
        self.root.destroy()

    def run(self):
        if sys.platform != "win32":
            self.status_var.set("注意：透明穿透为 Windows 特性，非 Windows 平台取词区可能不透明。")
        self.root.mainloop()


# ===========================================================================
# 7. 去重逻辑自测 (python video_word_grabber.py --selftest)
# ===========================================================================
def _run(stream, mode="grow"):
    dd = SubtitleDeduper(stream_mode=mode)
    out = []
    for cap in stream:
        line = dd.feed(cap)
        if line is not None:
            out.append(line)
    tail = dd.flush()
    if tail is not None:
        out.append(tail)
    return out


# 字幕类型 -> 去重模式
_MODE = {"typewriter": "grow", "sliding": "scroll", "whole-line": "line", "mixed": "grow"}

# 流式字幕对抗测试集(由设计/对抗工作流生成, 逐条人工核对预期输出)
_SELFTEST_CASES = [
    {
        "name": "typewriter_cn_char_by_char_with_blank_gap",
        "mode": "typewriter",
        "stream": [
            "今",
            "今天",
            "今天天",
            "今天天气",
            "今天天气真",
            "今天天气真好",
            "今天天气真好",
            "",
            "",
            "明天",
            "明天会",
            "明天会下",
            "明天会下雨",
            "明天会下雨",
            ""
        ],
        "expected": [
            "今天天气真好",
            "明天会下雨"
        ]
    },
    {
        "name": "typewriter_cn_no_gap_before_next_line",
        "mode": "typewriter",
        "stream": [
            "你",
            "你好",
            "你好世",
            "你好世界",
            "谢",
            "谢谢",
            "谢谢你",
            "谢谢你们",
            ""
        ],
        "expected": [
            "你好世界",
            "谢谢你们"
        ]
    },
    {
        "name": "word_by_word_en",
        "mode": "typewriter",
        "stream": [
            "The",
            "The quick",
            "The quick brown",
            "The quick brown fox",
            "The quick brown fox jumps",
            "The quick brown fox jumps",
            "",
            ""
        ],
        "expected": [
            "The quick brown fox jumps"
        ]
    },
    {
        "name": "fast_typewriter_few_partial_states",
        "mode": "typewriter",
        "stream": [
            "Wait",
            "Wait for me!",
            "Wait for me!",
            "",
            "Wait for me!"
        ],
        "expected": [
            "Wait for me!"
        ]
    },
    {
        "name": "typewriter_misread_char_mid_growth",
        "mode": "typewriter",
        "stream": [
            "速",
            "速度",
            "速喥很",
            "速度很快",
            "速度很快",
            "",
            ""
        ],
        "expected": [
            "速度很快"
        ]
    },
    {
        "name": "typewriter_dropped_char_mid_growth",
        "mode": "typewriter",
        "stream": [
            "我们",
            "我们一",
            "我们一起",
            "我一起去",
            "我们一起去",
            "我们一起去玩",
            "我们一起去玩",
            ""
        ],
        "expected": [
            "我们一起去玩"
        ]
    },
    {
        "name": "sliding_window_scroll_stitch",
        "mode": "sliding",
        "stream": [
            "今天天气",
            "今天天气很",
            "今天天气很好",
            "天气很好我们",
            "气很好我们出",
            "很好我们出去玩",
            "很好我们出去玩",
            "",
            ""
        ],
        "expected": [
            "今天天气很好我们出去玩"
        ]
    },
    {
        "name": "sliding_window_then_reset_new_scroll",
        "mode": "sliding",
        "stream": [
            "他走进",
            "他走进房间",
            "走进房间看",
            "进房间看到",
            "房间看到了",
            "",
            "下一个场景",
            "下一个场景开",
            "一个场景开始",
            "个场景开始了",
            "",
            ""
        ],
        "expected": [
            "他走进房间看到了",
            "下一个场景开始了"
        ]
    },
    {
        "name": "distinct_lines_shared_stem_chapter_1_to_4",
        "mode": "whole-line",
        "stream": [
            "Chapter 1",
            "Chapter 1",
            "",
            "",
            "Chapter 2",
            "Chapter 2",
            "",
            "",
            "Chapter 3",
            "",
            "",
            "Chapter 4",
            "Chapter 4",
            "",
            ""
        ],
        "expected": [
            "Chapter 1",
            "Chapter 2",
            "Chapter 3",
            "Chapter 4"
        ]
    },
    {
        "name": "single_char_cjk_interleaved_with_long_lines",
        "mode": "mixed",
        "stream": [
            "是",
            "是",
            "",
            "",
            "这是一个很长的句子",
            "这是一个很长的句子",
            "",
            "",
            "好",
            "好",
            "",
            "",
            "我们继续往下讲",
            "我们继续往下讲",
            ""
        ],
        "expected": [
            "是",
            "这是一个很长的句子",
            "好",
            "我们继续往下讲"
        ]
    },
    {
        "name": "legitimate_exact_repeat_far_apart",
        "mode": "whole-line",
        "stream": [
            "请稍候",
            "请稍候",
            "",
            "",
            "下载完成",
            "下载完成",
            "",
            "",
            "正在处理",
            "正在处理",
            "",
            "",
            "请稍候",
            "请稍候",
            "",
            ""
        ],
        "expected": [
            "请稍候",
            "下载完成",
            "正在处理",
            "请稍候"
        ]
    },
    {
        "name": "immediate_flicker_vanish_and_return",
        "mode": "whole-line",
        "stream": [
            "不要走开",
            "不要走开",
            "",
            "不要走开",
            "不要走开",
            "",
            ""
        ],
        "expected": [
            "不要走开"
        ]
    },
    {
        "name": "whole_line_subtitles_hello_world",
        "mode": "whole-line",
        "stream": [
            "Hello",
            "Hello",
            "Hello",
            "",
            "",
            "World",
            "World",
            "World",
            "",
            ""
        ],
        "expected": [
            "Hello",
            "World"
        ]
    },
    {
        "name": "all_blank_stream",
        "mode": "whole-line",
        "stream": [
            "",
            "",
            "",
            "",
            "",
            ""
        ],
        "expected": []
    }
]


def selftest() -> int:
    # 控制台用 UTF-8 输出, 避免 GBK 代码页无法显示中文(不影响文件写入)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    npass = 0
    for c in _SELFTEST_CASES:
        mode = _MODE.get(c["mode"], "grow")
        got = _run(c["stream"], mode)
        ok = got == c["expected"]
        npass += ok
        print(("[PASS]" if ok else "[FAIL]") + " (%s/%s) %s" % (c["mode"], mode, c["name"]))
        if not ok:
            print("    期望: %r" % c["expected"])
            print("    实际: %r" % got)
    total = len(_SELFTEST_CASES)
    print("")
    print("自测结果: %d/%d %s" % (
        npass, total, "全部通过 [OK]" if npass == total else "存在失败 [FAIL]"))
    return 0 if npass == total else 1


# ===========================================================================
def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    enable_dpi_awareness()   # 必须在 Tk() 之前
    App().run()


if __name__ == "__main__":
    main()
