"""一致连续性 —— 教学可视化视频生成脚本

内容按华东师范大学数学科学学院《数学分析》上册 第四章
“函数的连续性” §2 连续函数的性质 中“一致连续性”一节组织：
  连续性回顾 → 一致连续定义 → 几何意义 → 例题 → 否定形式与反例
  → 最大 δ(x0) 的下界 → 一致连续性定理（Cantor 定理）及证明 → 小结

依赖：numpy, matplotlib, ffmpeg（配音另需 soundfile，见 tts.py）
用法：
  python3 tts.py --model-dir <Kokoro 模型目录>   # 可选：先离线合成配音到 audio/
  python3 make_video.py [--out uniform_continuity.mp4] [--fps 30] [--scene N] [--preview]
有 audio/durations.json 时，各段时长会按配音长度自动拉伸，并把配音混入视频
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from multiprocessing import Pool

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyBboxPatch, Rectangle

# ---------------------------------------------------------------- 配音（由 tts.py 生成，可选）
HERE = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(HERE, "audio")
LEAD, TAIL = 0.3, 0.6  # 每段配音前的留白、后的停顿（秒）
try:
    with open(os.path.join(AUDIO_DIR, "durations.json")) as _fh:
        AUDIO_DURATIONS = json.load(_fh)
except FileNotFoundError:
    AUDIO_DURATIONS = {}

# ---------------------------------------------------------------- 样式
W, H, DPI = 1280, 720, 100
BG = "#14171f"
FG = "#e8e6e3"
DIM = "#8a8f98"
BLUE = "#58c4dd"
YELLOW = "#f4d35e"
GREEN = "#83c167"
RED = "#fc6255"
ORANGE = "#ff9f43"
PURPLE = "#b48ead"

plt.rcParams.update({
    "font.family": ["WenQuanYi Zen Hei", "DejaVu Sans"],
    "mathtext.fontset": "cm",
    "axes.unicode_minus": False,
    "text.color": FG,
    "axes.edgecolor": DIM,
    "axes.labelcolor": FG,
    "xtick.color": DIM,
    "ytick.color": DIM,
})


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def smooth(x):
    x = clamp(x)
    return x * x * (3 - 2 * x)


def ramp(t, t0, t1):
    """t 在 [t0,t1] 内从 0 平滑过渡到 1"""
    if t1 <= t0:
        return 1.0 if t >= t0 else 0.0
    return smooth((t - t0) / (t1 - t0))


def fade(t, t0, t1, dur=0.5):
    """在 [t0, t1] 内可见，两端淡入淡出"""
    return min(ramp(t, t0, t0 + dur), 1 - ramp(t, t1 - dur, t1))


# ---------------------------------------------------------------- 场景框架
class Scene:
    """每个场景由若干条字幕段组成；字幕段的起止时刻作为动画的时间锚点。"""

    title = ""
    subs = []  # [(时长秒, 字幕文本)]

    def __init__(self):
        # 有配音时，每段时长取“预设时长”与“配音时长+留白”中的较大者
        audio = AUDIO_DURATIONS.get(type(self).__name__, [])
        self.segs = [(max(d, LEAD + audio[i] + TAIL) if i < len(audio) else d, text)
                     for i, (d, text) in enumerate(self.subs)]
        self.starts = []
        acc = 0.0
        for d, _ in self.segs:
            self.starts.append(acc)
            acc += d
        self.duration = acc

    def k(self, i):
        """第 i 段字幕开始的时刻（i 可等于段数，表示场景结尾）"""
        return self.starts[i] if i < len(self.starts) else self.duration

    def new_fig(self):
        fig = plt.figure(figsize=(W / DPI, H / DPI), dpi=DPI, facecolor=BG)
        return fig

    def chrome(self, fig, t):
        """标题与底部字幕"""
        if self.title:
            fig.text(0.035, 0.945, self.title, fontsize=17, color=DIM, va="center")
        for i, (d, s) in enumerate(self.segs):
            t0 = self.starts[i]
            if t0 <= t < t0 + d:
                a = fade(t, t0, t0 + d, 0.25)
                fig.patches.append(FancyBboxPatch(
                    (0.06, 0.018), 0.88, 0.075, boxstyle="round,pad=0.004,rounding_size=0.01",
                    transform=fig.transFigure, facecolor="#000000", alpha=0.55 * a,
                    edgecolor="none", zorder=50))
                fig.text(0.5, 0.055, s, ha="center", va="center", fontsize=19,
                         family=["DejaVu Sans", "WenQuanYi Zen Hei"],
                         color=FG, alpha=a, zorder=51)

    def draw(self, fig, t):
        raise NotImplementedError

    def frame(self, t):
        fig = self.new_fig()
        self.draw(fig, t)
        self.chrome(fig, t)
        fig.canvas.draw()
        buf = np.asarray(fig.canvas.buffer_rgba())[:, :, :3].copy()
        plt.close(fig)
        return buf


def make_axes(fig, rect, xlim, ylim, xlabel="x", ylabel="y"):
    ax = fig.add_axes(rect, facecolor=BG)
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(labelsize=11)
    ax.grid(True, color="#262b36", lw=0.8)
    ax.set_axisbelow(True)
    return ax


def measure(fig, s, fontsize):
    txt = fig.text(0, 0, s, fontsize=fontsize)
    bb = txt.get_window_extent(fig.canvas.get_renderer()).transformed(fig.transFigure.inverted())
    txt.remove()
    return bb.width


def rich_line(fig, x, y, parts, fontsize, alpha=1.0, starts=None, gap=0.012):
    """把若干段文字/公式依次排成一行（基线对齐），返回各段在 figure 坐标中的包围盒。
    parts: [(文本, 颜色)]；starts 可指定前若干段的起始横坐标（用于两行按列对齐）"""
    r = fig.canvas.get_renderer()
    boxes = []
    for i, (s, c) in enumerate(parts):
        if starts and i < len(starts):
            x = starts[i]
        txt = fig.text(x, y, s, fontsize=fontsize, color=c, alpha=alpha, va="baseline")
        bb = txt.get_window_extent(r).transformed(fig.transFigure.inverted())
        boxes.append(bb)
        x = bb.x1 + gap
    return boxes


def column_starts(fig, x, rows, ncol, fontsize, gap=0.012):
    """多行公式前 ncol 段按列对齐时，各列的起始横坐标"""
    starts = []
    for j in range(ncol):
        starts.append(x)
        x += max(measure(fig, row[j], fontsize) for row in rows) + gap
    return starts


def frame_box(fig, bb, color, alpha, pad=0.006):
    fig.patches.append(FancyBboxPatch((bb.x0 - pad, bb.y0 - pad), bb.width + 2 * pad,
                                      bb.height + 2 * pad, boxstyle="round,pad=0.002",
                                      transform=fig.transFigure, facecolor="none",
                                      edgecolor=color, lw=2, alpha=alpha))


def oscillation(f, a, b, n=200):
    xs = np.linspace(a, b, n)
    ys = f(xs)
    return ys.min(), ys.max()


def sliding_box(ax, f, c, delta, eps, lo, hi, alpha=1.0, label=True):
    """宽 δ、高 ε 的窗口：窗口内振幅 < ε 为绿色（满足），否则红色。返回是否满足"""
    a, b = c - delta / 2, c + delta / 2
    a, b = max(a, lo), min(b, hi)
    ymin, ymax = oscillation(f, a, b)
    ok = (ymax - ymin) < eps
    col = GREEN if ok else RED
    mid = (ymin + ymax) / 2
    ax.add_patch(Rectangle((a, mid - eps / 2), b - a, eps, facecolor=col, alpha=0.18 * alpha,
                           edgecolor=col, lw=2, zorder=5))
    xs = np.linspace(a, b, 120)
    ax.plot(xs, f(xs), color=col, lw=3.2, alpha=alpha, zorder=6)
    if label:
        ax.annotate("", xy=(a, mid - eps / 2), xytext=(b, mid - eps / 2), alpha=alpha,
                    arrowprops=dict(arrowstyle="<->", color=YELLOW, lw=1.4))
    return ok, (a, b, ymin, ymax)


# ---------------------------------------------------------------- 场景 1：片头
class S01Title(Scene):
    subs = [
        (4.5, "本视频配合《数学分析》第四章 §2“连续函数的性质”学习"),
        (4.5, "主题：一致连续性 —— 从“逐点”到“整体”的连续"),
    ]

    def draw(self, fig, t):
        a = ramp(t, 0.2, 1.4)
        fig.text(0.5, 0.64, "一致连续性", ha="center", fontsize=60, color=FG, alpha=a,
                 fontweight="bold")
        fig.text(0.5, 0.53, "Uniform Continuity", ha="center", fontsize=24, color=BLUE, alpha=a)
        b = ramp(t, 1.2, 2.4)
        fig.text(0.5, 0.40, "数学分析 · 第四章 函数的连续性 · §2 连续函数的性质",
                 ha="center", fontsize=18, color=DIM, alpha=b)
        fig.text(0.5, 0.34, "参考教材：华东师范大学数学科学学院《数学分析》上册",
                 ha="center", fontsize=15, color=DIM, alpha=b)
        # 一条曲线在下方慢慢画出，带一个滑动窗口
        ax = fig.add_axes([0.15, 0.13, 0.7, 0.16], facecolor=BG)
        ax.axis("off")
        ax.set_xlim(0, 2 * np.pi)
        ax.set_ylim(-1.3, 1.3)
        p = ramp(t, 0.5, 6)
        xs = np.linspace(0, 2 * np.pi * p + 1e-6, 300)
        ax.plot(xs, np.sin(2 * xs) * 0.9, color=BLUE, lw=2.5, alpha=a)
        if p > 0.05:
            sliding_box(ax, lambda x: 0.9 * np.sin(2 * x), 2 * np.pi * p - 0.2, 0.35, 0.9,
                        0, 2 * np.pi, alpha=a, label=False)


# ---------------------------------------------------------------- 场景 2：回顾连续
class S02Review(Scene):
    title = "一、回顾：函数在一点连续"
    subs = [
        (5.0, "f 在点 x₀ 连续：任给 ε>0，存在 δ>0，当 |x−x₀|<δ 时 |f(x)−f(x₀)|<ε"),
        (5.0, "以 f(x)=1/x 为例：先固定 ε，围绕 f(x₀) 画出高为 2ε 的水平带"),
        (5.0, "再找 δ，使 (x₀−δ, x₀+δ) 上的图像全部落在水平带内"),
        (7.0, "把 x₀ 向 0 移动：同一个 ε，所需的 δ 越来越小"),
        (5.0, "所以 δ 不仅依赖于 ε，还依赖于点 x₀：δ = δ(ε, x₀)"),
    ]

    f = staticmethod(lambda x: 1 / x)

    @staticmethod
    def delta_max(x0, eps):
        # |1/x − 1/x0| < ε 在 x0 左侧更紧：1/x − 1/x0 < ε ⇔ x > x0/(1+ε x0)
        left = x0 - x0 / (1 + eps * x0)
        right = (x0 / (1 - eps * x0) - x0) if eps * x0 < 1 else np.inf
        return min(left, right)

    def draw(self, fig, t):
        fig.text(0.62, 0.84,
                 r"$\forall\varepsilon>0,\ \exists\delta>0,\ |x-x_0|<\delta"
                 r"\Rightarrow|f(x)-f(x_0)|<\varepsilon$",
                 fontsize=21, ha="center", color=FG, alpha=ramp(t, 0, 0.8))
        ax = make_axes(fig, [0.08, 0.15, 0.62, 0.62], (0, 2.2), (0, 7.5))
        xs = np.linspace(0.12, 2.2, 400)
        ax.plot(xs, 1 / xs, color=BLUE, lw=2.5)
        ax.text(1.7, 5.5, r"$y=\dfrac{1}{x}$", color=BLUE, fontsize=20)
        eps = 0.6
        x0 = 1.5 - 1.25 * ramp(t, self.k(3) + 0.3, self.k(4) - 0.5)
        y0 = 1 / x0
        d = self.delta_max(x0, eps) * 0.97
        ea = ramp(t, self.k(1), self.k(1) + 1)
        da = ramp(t, self.k(2), self.k(2) + 1)
        ax.axhspan(y0 - eps, y0 + eps, color=YELLOW, alpha=0.15 * ea, lw=0)
        ax.axhline(y0 + eps, color=YELLOW, lw=1, alpha=0.7 * ea, ls="--")
        ax.axhline(y0 - eps, color=YELLOW, lw=1, alpha=0.7 * ea, ls="--")
        ax.axvspan(x0 - d, x0 + d, color=GREEN, alpha=0.18 * da, lw=0)
        seg = np.linspace(x0 - d, x0 + d, 100)
        ax.plot(seg, 1 / seg, color=GREEN, lw=4, alpha=da)
        ax.plot([x0], [y0], "o", color=FG, ms=7, zorder=10)
        ax.plot([x0, x0], [0, y0], color=FG, lw=1, ls=":")
        ax.text(x0 + 0.03, 0.15, r"$x_0$", ha="left", fontsize=17, color=FG, zorder=10)
        if ea > 0:
            ax.text(2.12, y0 + eps + 0.1, r"$f(x_0)\pm\varepsilon$", color=YELLOW, fontsize=15,
                    ha="right", alpha=ea)

        # 右侧数值面板
        pa = ramp(t, self.k(2), self.k(2) + 1)
        fig.text(0.75, 0.66, r"$\varepsilon = 0.6$", fontsize=22, color=YELLOW, alpha=pa)
        fig.text(0.75, 0.58, rf"$x_0 = {x0:.3f}$", fontsize=22, color=FG, alpha=pa)
        fig.text(0.75, 0.50, rf"$\delta_{{\max}} \approx {d / 0.97:.4f}$", fontsize=22,
                 color=GREEN, alpha=pa)
        qa = ramp(t, self.k(4), self.k(4) + 1)
        fig.text(0.75, 0.36, r"$\delta=\delta(\varepsilon,\,x_0)$", fontsize=28, color=ORANGE,
                 alpha=qa)
        fig.text(0.75, 0.29, "δ 随 x₀ 变化", fontsize=18, color=ORANGE, alpha=qa)


# ---------------------------------------------------------------- 场景 3：定义
class S03Definition(Scene):
    title = "二、一致连续的定义"
    subs = [
        (5.0, "问题：能否找到一个只与 ε 有关、对区间 I 上所有点都适用的 δ？"),
        (6.0, "定义：设 f 定义在区间 I 上。若对任给 ε>0，存在 δ=δ(ε)>0，"),
        (6.0, "使得对任意 x′, x″∈I，只要 |x′−x″|<δ，就有 |f(x′)−f(x″)|<ε"),
        (6.5, "则称 f 在 I 上一致连续。关键在于量词顺序：δ 在“任意 x”之前选定"),
        (5.5, "一致连续 ⇒ 连续；反之不一定成立"),
    ]

    def draw(self, fig, t):
        fig.text(0.5, 0.82, "能否选一个对所有点都通用的 δ ？", ha="center", fontsize=28,
                 color=YELLOW, alpha=fade(t, 0, self.k(1) + 0.6))
        a1 = ramp(t, self.k(1), self.k(1) + 1)
        a2 = ramp(t, self.k(1) + 0.8, self.k(2) + 0.5)
        fs = 21
        row1 = [r"$\forall x_0\in I,\ \forall\varepsilon>0,$", r"$\exists\delta>0,$",
                r"$\forall x\in I:\ |x-x_0|<\delta\Rightarrow|f(x)-f(x_0)|<\varepsilon$"]
        row2 = [r"$\forall\varepsilon>0,$", r"$\exists\delta>0,$",
                r"$\forall x',x''\in I:\ |x'-x''|<\delta\Rightarrow|f(x')-f(x'')|<\varepsilon$"]
        fig.text(0.05, 0.66, "在 I 上连续：", fontsize=22, color=DIM, alpha=a1, va="baseline")
        fig.text(0.05, 0.50, "在 I 上一致连续：", fontsize=22, color=FG, alpha=a2, va="baseline")
        b1 = rich_line(fig, 0.25, 0.66, [(x, DIM) for x in row1], fs, alpha=a1)
        b2 = rich_line(fig, 0.25, 0.50, [(x, FG) for x in row2], fs, alpha=a2)
        h = ramp(t, self.k(3), self.k(3) + 0.8)
        if h > 0:
            frame_box(fig, b1[1], ORANGE, h)
            frame_box(fig, b2[1], GREEN, h)
            fig.text(b1[1].x0, b1[1].y0 - 0.05, r"δ 在 $x_0$ 之后选 → 可依赖 $x_0$",
                     fontsize=16, color=ORANGE, alpha=h)
            fig.text(b2[1].x0, b2[1].y0 - 0.05, r"δ 在 $x',x''$ 之前选 → 只依赖 $\varepsilon$",
                     fontsize=16, color=GREEN, alpha=h)
        a4 = ramp(t, self.k(4), self.k(4) + 0.8)
        fig.text(0.5, 0.30, "一致连续  ⟹  连续", ha="center", fontsize=30, color=GREEN, alpha=a4)
        fig.text(0.5, 0.22, "连续  ⟹̸  一致连续", ha="center", fontsize=30, color=RED, alpha=a4)


# ---------------------------------------------------------------- 场景 4：几何意义
class S04Geometry(Scene):
    title = "三、几何意义：宽 δ、高 ε 的滑动窗口"
    subs = [
        (6.0, "取一个宽为 δ、高为 ε 的矩形窗口，让它沿着曲线从左到右滑动"),
        (6.0, "若曲线始终只从窗口左右两侧穿出、不碰上下边，窗口内任意两点函数值之差 < ε"),
        (6.0, "以 f(x)=√x 在 [0,4] 上为例：取 ε=0.5，δ=0.2 便处处满足"),
        (6.0, "注意：√x 在 0 处切线竖直，导数无界，但它依然是一致连续的"),
        (5.0, "一致连续：对每个 ε，都能找到一个 δ，使窗口沿整条曲线畅通无阻"),
    ]

    def draw(self, fig, t):
        f = np.sqrt
        ax = make_axes(fig, [0.08, 0.17, 0.58, 0.66], (-0.1, 4.2), (-0.2, 2.4))
        xs = np.linspace(0, 4, 500)
        ax.plot(xs, f(xs), color=BLUE, lw=2.5)
        ax.text(3.0, 1.1, r"$y=\sqrt{x}$", color=BLUE, fontsize=20)
        eps, delta = 0.5, 0.2
        p = (t % 12.0) / 12.0
        c = 0.1 + p * 3.8
        ok, (a, b, ymin, ymax) = sliding_box(ax, f, c, delta, eps, 0, 4)
        ax.text((a + b) / 2, (ymin + ymax) / 2 - eps / 2 - 0.17, r"$\delta$", color=YELLOW,
                fontsize=17, ha="center")
        ax.text(b + 0.06, (ymin + ymax) / 2, r"$\varepsilon$", color=YELLOW, fontsize=17,
                va="center")
        # 右侧说明
        fig.text(0.70, 0.76, r"$\varepsilon=0.5,\quad \delta=0.2$", fontsize=22, color=YELLOW)
        fig.text(0.70, 0.67, "窗口内振幅：", fontsize=19, color=FG)
        fig.text(0.70, 0.60, rf"$\max-\min = {ymax - ymin:.3f}$", fontsize=22,
                 color=GREEN if ok else RED)
        a3 = ramp(t, self.k(2), self.k(2) + 1)
        fig.text(0.70, 0.48, r"$|\sqrt{x'}-\sqrt{x''}|\leq\sqrt{|x'-x''|}$", fontsize=21,
                 color=FG, alpha=a3)
        fig.text(0.70, 0.40, r"取 $\delta=\varepsilon^2$ 即可", fontsize=20, color=FG, alpha=a3)
        a4 = ramp(t, self.k(3), self.k(3) + 1)
        fig.text(0.70, 0.29, "导数无界 ≠ 不一致连续", fontsize=20, color=ORANGE, alpha=a4)
        if a4 > 0:
            ax.plot([0, 0], [0, 0.6], color=ORANGE, lw=2, alpha=a4, ls="--")
            ax.text(0.08, 0.5, "切线竖直", color=ORANGE, fontsize=14, alpha=a4)


# ---------------------------------------------------------------- 场景 5：例 1 sin x
class S05Sin(Scene):
    title = "例 1  证明 sin x 在 (−∞, +∞) 上一致连续"
    subs = [
        (6.0, "对任意 x′, x″，由和差化积公式与 |sin u| ≤ |u|，"),
        (6.0, "得 |sin x′ − sin x″| = 2|cos((x′+x″)/2)|·|sin((x′−x″)/2)| ≤ |x′ − x″|"),
        (5.0, "于是任给 ε>0，取 δ=ε，就有 |x′−x″|<δ ⇒ |sin x′ − sin x″|<ε"),
        (6.0, "δ=ε 与点的位置无关：窗口在整条正弦曲线上滑动都满足条件"),
    ]

    def draw(self, fig, t):
        a0 = ramp(t, 0, 1)
        fig.text(0.07, 0.82,
                 r"$|\sin x'-\sin x''|=2\left|\cos\frac{x'+x''}{2}\right|\cdot"
                 r"\left|\sin\frac{x'-x''}{2}\right|$", fontsize=23, alpha=a0)
        a1 = ramp(t, self.k(1), self.k(1) + 1)
        fig.text(0.255, 0.735, r"$\leq 2\cdot 1\cdot\frac{|x'-x''|}{2}=|x'-x''|$", fontsize=23,
                 alpha=a1)
        a2 = ramp(t, self.k(2), self.k(2) + 1)
        fig.text(0.62, 0.78, r"取 $\delta=\varepsilon$", fontsize=28, color=GREEN, alpha=a2)
        ax = make_axes(fig, [0.07, 0.15, 0.86, 0.48], (-7, 7), (-1.6, 1.6))
        xs = np.linspace(-7, 7, 700)
        ax.plot(xs, np.sin(xs), color=BLUE, lw=2.5)
        a3 = ramp(t, self.k(2) + 0.5, self.k(2) + 1.5)
        if a3 > 0:
            eps = 0.6
            c = -6.5 + 13 * ((t - self.k(2)) % 9) / 9
            sliding_box(ax, np.sin, c, eps * 0.999, eps, -7, 7, alpha=a3)


# ---------------------------------------------------------------- 场景 6：否定形式
class S06Negation(Scene):
    title = "四、不一致连续的否定形式"
    subs = [
        (6.0, "把定义中的量词逐个否定：∀ 换成 ∃，∃ 换成 ∀，结论取反"),
        (6.0, "f 在 I 上不一致连续 ⇔ 存在 ε₀>0，对任何 δ>0，都存在 x′, x″∈I，"),
        (5.0, "虽然 |x′−x″|<δ，但 |f(x′)−f(x″)| ≥ ε₀"),
        (7.0, "常用的序列形式：存在两列点 x′ₙ, x″ₙ，x′ₙ − x″ₙ → 0，但 |f(x′ₙ)−f(x″ₙ)| ≥ ε₀"),
    ]

    def draw(self, fig, t):
        fs = 21
        row1 = [r"$\forall\varepsilon>0,$", r"$\exists\delta>0,$", r"$\forall x',x''\in I:$",
                r"$|x'-x''|<\delta\ \Rightarrow\ |f(x')-f(x'')|<\varepsilon$"]
        row2 = [r"$\exists\varepsilon_0>0,$", r"$\forall\delta>0,$", r"$\exists x',x''\in I:$",
                r"$|x'-x''|<\delta$", "但", r"$|f(x')-f(x'')|\geq\varepsilon_0$"]
        starts = column_starts(fig, 0.24, [row1, row2], 3, fs)
        fig.text(0.05, 0.78, "一致连续：", fontsize=22, color=DIM, va="baseline")
        b1 = rich_line(fig, 0, 0.78, [(x, DIM) for x in row1], fs, starts=starts)
        a1 = ramp(t, self.k(1), self.k(2) + 0.8)
        fig.text(0.05, 0.60, "不一致连续：", fontsize=22, color=RED, alpha=a1, va="baseline")
        cols = [RED, RED, RED, FG, FG, RED]
        b2 = rich_line(fig, 0, 0.60, list(zip(row2, cols)), fs, alpha=a1, starts=starts)
        # 量词变换提示
        q = ramp(t, self.k(0) + 1, self.k(1))
        for bb, lab in zip(b1[:3], [r"$\forall\to\exists$", r"$\exists\to\forall$",
                                    r"$\forall\to\exists$"]):
            fig.text((bb.x0 + bb.x1) / 2, 0.69, lab, fontsize=16, color=ORANGE, alpha=q,
                     ha="center", va="center")
        fig.text((b1[3].x0 + b1[3].x1) / 2, 0.69, "结论取反", fontsize=16, color=ORANGE,
                 alpha=q, ha="center", va="center")
        a3 = ramp(t, self.k(3), self.k(3) + 1)
        fig.patches.append(FancyBboxPatch((0.07, 0.30), 0.86, 0.17, boxstyle="round,pad=0.01",
                                          transform=fig.transFigure, facecolor="#1d2330",
                                          edgecolor=YELLOW, lw=1.5, alpha=a3))
        fig.text(0.5, 0.42, "序列形式（证明“不一致连续”的利器）", ha="center", fontsize=19,
                 color=YELLOW, alpha=a3)
        fig.text(0.5, 0.34,
                 r"$\exists\,\varepsilon_0>0,\ \{x_n'\},\{x_n''\}\subset I:\quad "
                 r"x_n'-x_n''\to 0,\quad |f(x_n')-f(x_n'')|\geq\varepsilon_0$",
                 ha="center", fontsize=22, alpha=a3)


# ---------------------------------------------------------------- 场景 7：反例 1/x
class S07Reciprocal(Scene):
    title = "例 2  f(x)=1/x 在 (0,1) 上连续，但不一致连续"
    subs = [
        (6.0, "固定 ε=1，取一个看起来很小的 δ，让窗口从右向左滑向 0"),
        (6.0, "靠近 0 时曲线越来越陡，窗口内振幅超过 ε，窗口变红：这个 δ 失败了"),
        (6.0, "无论 δ 多小，总能在更靠近 0 的地方失败 —— 不存在通用的 δ"),
        (7.0, "用序列形式严格证明：取 x′ₙ=1/n，x″ₙ=1/(n+1)，则 |x′ₙ−x″ₙ|=1/(n(n+1))→0"),
        (6.0, "但 |f(x′ₙ)−f(x″ₙ)| = |n−(n+1)| = 1 = ε₀，故 1/x 在 (0,1) 上不一致连续"),
        (6.0, "对比：在 [r,1]（0<r<1）上，|1/x′−1/x″| ≤ |x′−x″|/r²，取 δ=r²ε 即一致连续"),
    ]

    def draw(self, fig, t):
        f = lambda x: 1 / x
        ax = make_axes(fig, [0.08, 0.17, 0.56, 0.68], (0, 1.05), (0, 12))
        xs = np.linspace(0.06, 1.05, 500)
        ax.plot(xs, f(xs), color=BLUE, lw=2.5)
        ax.text(0.8, 2.0, r"$y=\dfrac{1}{x}$", color=BLUE, fontsize=20)
        eps = 1.0
        if t < self.k(3):
            # 两个 δ 依次演示
            if t < self.k(2):
                delta = 0.08
                p = ramp(t, 0.5, self.k(2) - 0.3)
            else:
                delta = 0.03
                p = ramp(t, self.k(2) + 0.2, self.k(3) - 0.3)
            c = 0.95 - p * (0.95 - 0.12 - delta / 2)
            ok, (a, b, ymin, ymax) = sliding_box(ax, f, c, delta, eps, 0.06, 1.05)
            fig.text(0.68, 0.76, r"$\varepsilon = 1$", fontsize=24, color=YELLOW)
            fig.text(0.68, 0.68, rf"$\delta = {delta}$", fontsize=24, color=YELLOW)
            fig.text(0.68, 0.58, rf"振幅 $= {ymax - ymin:.2f}$", fontsize=22,
                     color=GREEN if ok else RED)
            fig.text(0.68, 0.50, "满足" if ok else "失败！", fontsize=26,
                     color=GREEN if ok else RED, fontweight="bold")
        elif t < self.k(5):
            # 点列演示
            n_show = 1 + int(8 * ramp(t, self.k(3) + 0.5, self.k(4) + 2))
            for n in range(1, n_show + 1):
                if n < 2:
                    continue
                x1, x2 = 1 / n, 1 / (n + 1)
                al = 0.35 + 0.65 * (n == n_show)
                ax.plot([x1, x2], [n, n + 1], "o", color=ORANGE, ms=7, alpha=al, zorder=8)
                ax.plot([x1, x1], [0, n], color=ORANGE, lw=0.8, ls=":", alpha=al)
                ax.plot([x2, x2], [0, n + 1], color=ORANGE, lw=0.8, ls=":", alpha=al)
                ax.plot([x2, x2], [n, n + 1], color=RED, lw=3, alpha=al)
            n = max(n_show, 2)
            fig.text(0.68, 0.76, rf"$n={n}$", fontsize=24, color=ORANGE)
            fig.text(0.68, 0.67, rf"$x_n'=\frac{{1}}{{{n}}},\ x_n''=\frac{{1}}{{{n + 1}}}$",
                     fontsize=23)
            fig.text(0.68, 0.57, rf"$|x_n'-x_n''|=\frac{{1}}{{{n * (n + 1)}}}\to 0$", fontsize=23,
                     color=GREEN)
            a4 = ramp(t, self.k(4), self.k(4) + 0.8)
            fig.text(0.68, 0.46, r"$|f(x_n')-f(x_n'')|=1$", fontsize=23, color=RED, alpha=a4)
            fig.text(0.68, 0.38, r"$\varepsilon_0 = 1$", fontsize=23, color=RED, alpha=a4)
        else:
            r = 0.3
            ax.axvspan(r, 1, color=GREEN, alpha=0.08)
            ax.axvline(r, color=GREEN, ls="--", lw=1.2)
            ax.text(r, 11.2, r"$x=r$", color=GREEN, fontsize=16, ha="center")
            delta = 0.08
            c = r + delta / 2 + ((t - self.k(5)) % 4) / 4 * (1 - r - delta)
            sliding_box(ax, f, c, delta, eps, r, 1)
            fig.text(0.68, 0.74, r"在 $[r,1]$ 上：", fontsize=22, color=GREEN)
            fig.text(0.68, 0.64,
                     r"$\left|\frac{1}{x'}-\frac{1}{x''}\right|=\frac{|x'-x''|}{x'x''}"
                     r"\leq\frac{|x'-x''|}{r^2}$", fontsize=22)
            fig.text(0.68, 0.53, r"取 $\delta=r^2\varepsilon$", fontsize=22, color=GREEN)
            fig.text(0.68, 0.42, "问题出在“开端点” 0 附近", fontsize=19, color=ORANGE)


# ---------------------------------------------------------------- 场景 8：更多反例
class S08MoreCounter(Scene):
    title = "例 3  更多不一致连续的例子"
    subs = [
        (6.0, "sin(1/x) 在 (0,1) 上：越靠近 0 振荡越快，函数值在 −1 与 1 之间来回跳"),
        (6.0, "取 x′ₙ=1/(2nπ+π/2)，x″ₙ=1/(2nπ)，两点距离 → 0，函数值之差恒为 1"),
        (6.0, "x² 在 [0,+∞) 上：问题不在端点，而在无穷远处 —— 曲线越来越陡"),
        (6.0, "取 x′ₙ=n+1/n，x″ₙ=n，则 x′ₙ−x″ₙ=1/n→0，但 f(x′ₙ)−f(x″ₙ)=2+1/n² > 2"),
    ]

    def draw(self, fig, t):
        left = t < self.k(2)
        if left:
            f = lambda x: np.sin(1 / x)
            ax = make_axes(fig, [0.08, 0.17, 0.56, 0.68], (0, 0.6), (-1.5, 1.5))
            xs = np.linspace(0.004, 0.6, 6000)
            ax.plot(xs, f(xs), color=BLUE, lw=1.6)
            ax.text(0.42, 1.15, r"$y=\sin\frac{1}{x}$", color=BLUE, fontsize=20)
            if t < self.k(1):
                delta = 0.03
                p = ramp(t, 0.3, self.k(1) - 0.2)
                c = 0.55 - p * 0.5
                sliding_box(ax, f, c, delta, 1.0, 0.004, 0.6)
                fig.text(0.68, 0.70, r"$\varepsilon=1,\ \delta=0.03$", fontsize=22, color=YELLOW)
            else:
                m = 1 + int(4 * ramp(t, self.k(1) + 0.3, self.k(2) - 0.8))
                for n in range(1, m + 1):
                    x1, x2 = 1 / (2 * n * np.pi + np.pi / 2), 1 / (2 * n * np.pi)
                    al = 0.4 + 0.6 * (n == m)
                    ax.plot([x1], [1], "o", color=ORANGE, ms=7, alpha=al, zorder=8)
                    ax.plot([x2], [0], "o", color=GREEN, ms=7, alpha=al, zorder=8)
                    ax.plot([x1, x2], [1, 0], color=RED, lw=1.5, alpha=al)
                fig.text(0.68, 0.72, r"$x_n'=\frac{1}{2n\pi+\pi/2}$", fontsize=22, color=ORANGE)
                fig.text(0.68, 0.63, r"$x_n''=\frac{1}{2n\pi}$", fontsize=22, color=GREEN)
                fig.text(0.68, 0.53, r"$x_n'-x_n''\to 0$", fontsize=22)
                fig.text(0.68, 0.45, r"$|f(x_n')-f(x_n'')|=1$", fontsize=22, color=RED)
        else:
            f = lambda x: x ** 2
            ax = make_axes(fig, [0.08, 0.17, 0.56, 0.68], (0, 6), (0, 36))
            xs = np.linspace(0, 6, 500)
            ax.plot(xs, f(xs), color=BLUE, lw=2.5)
            ax.text(1.0, 20, r"$y=x^2$", color=BLUE, fontsize=20)
            if t < self.k(3):
                delta, eps = 0.25, 2.0
                p = ramp(t, self.k(2) + 0.3, self.k(3) - 0.2)
                c = 0.3 + p * 5.4
                ok, (a, b, ymin, ymax) = sliding_box(ax, f, c, delta, eps, 0, 6)
                fig.text(0.68, 0.70, r"$\varepsilon=2,\ \delta=0.25$", fontsize=22, color=YELLOW)
                fig.text(0.68, 0.60, rf"振幅 $= {ymax - ymin:.2f}$", fontsize=22,
                         color=GREEN if ok else RED)
            else:
                m = 1 + int(4 * ramp(t, self.k(3) + 0.3, self.k(4) - 1))
                for n in range(1, m + 1):
                    x1, x2 = n + 1 / n, n
                    al = 0.4 + 0.6 * (n == m)
                    ax.plot([x1, x2], [f(x1), f(x2)], "o", color=ORANGE, ms=7, alpha=al)
                    ax.plot([x1, x1], [f(x2), f(x1)], color=RED, lw=3, alpha=al)
                fig.text(0.68, 0.72, r"$x_n'=n+\frac{1}{n},\ x_n''=n$", fontsize=22,
                         color=ORANGE)
                fig.text(0.68, 0.62, r"$x_n'-x_n''=\frac{1}{n}\to 0$", fontsize=22)
                fig.text(0.68, 0.52, r"$f(x_n')-f(x_n'')=2+\frac{1}{n^2}>2$", fontsize=22,
                         color=RED)
                fig.text(0.68, 0.40, "取 ε₀ = 2", fontsize=22, color=RED)


# ---------------------------------------------------------------- 场景 9：δ(x0) 的下界
def delta_star(f, x0, eps, lo, hi, n=4000):
    """数值计算 x0 处能用的最大 δ：sup{δ : |x−x0|<δ, x∈[lo,hi] ⇒ |f(x)−f(x0)|<ε}"""
    span = hi - lo
    xs = np.linspace(lo, hi, n)
    bad = np.abs(f(xs) - f(x0)) >= eps
    if not bad.any():
        return span
    return np.abs(xs[bad] - x0).min()


class S09DeltaCurve(Scene):
    title = "五、换个角度：每一点的最大 δ"
    subs = [
        (6.0, "固定 ε，记 δ*(x₀) 为点 x₀ 处可用的最大 δ。画出 δ*(x₀) 随 x₀ 变化的曲线"),
        (6.0, "f(x)=√x 在 [0,4] 上：δ*(x₀) 有正的最小值，这个最小值就是通用的 δ"),
        (6.0, "f(x)=1/x 在 (0,1) 上：x₀→0⁺ 时 δ*(x₀)→0，下确界为 0，找不到通用的 δ"),
        (6.0, "结论：f 在 I 上一致连续 ⇔ 对每个 ε>0，inf δ*(x₀) > 0"),
    ]

    def draw(self, fig, t):
        eps = 0.5
        first = t < self.k(2)
        if first:
            f, lo, hi, name, ylim = np.sqrt, 0.0, 4.0, r"$f(x)=\sqrt{x},\ x\in[0,4]$", (0, 2.2)
            x0s = np.linspace(0, 4, 160)
        else:
            f, lo, hi, name, ylim = (lambda x: 1 / x), 0.02, 1.0, \
                r"$f(x)=\frac{1}{x},\ x\in(0,1)$", (0, 12)
            x0s = np.linspace(0.08, 1.0, 160)
        ds = np.array([delta_star(f, x, eps, lo, hi) for x in x0s])
        ax1 = make_axes(fig, [0.08, 0.54, 0.55, 0.28], (x0s[0] - 0.02 if first else 0, hi), ylim)
        xs = np.linspace(max(lo, 1e-3), hi, 500)
        ax1.plot(xs, f(xs), color=BLUE, lw=2.5)
        ax1.set_title(name, color=BLUE, fontsize=18, loc="left")
        ax2 = make_axes(fig, [0.08, 0.17, 0.55, 0.25], ax1.get_xlim(), (0, ds.max() * 1.15))
        ax2.set_title(r"$\delta^*(x_0)$,  $\varepsilon=0.5$", color=GREEN, fontsize=18,
                      loc="left")
        seg0 = 0 if first else self.k(2)
        p = ramp(t, seg0 + 0.5, seg0 + 4.5)
        m = max(2, int(p * len(x0s)))
        ax2.plot(x0s[:m], ds[:m], color=GREEN, lw=2.5)
        i = m - 1
        x0 = x0s[i]
        d = ds[i]
        ax1.plot([x0], [f(x0)], "o", color=FG, ms=6)
        ax1.axvspan(max(x0 - d, ax1.get_xlim()[0]), min(x0 + d, hi), color=GREEN, alpha=0.15)
        ax1.axhspan(f(x0) - eps, f(x0) + eps, color=YELLOW, alpha=0.12)
        ax2.plot([x0], [d], "o", color=FG, ms=6)
        if first and t > self.k(1):
            j = ds.argmin()
            a = ramp(t, self.k(1), self.k(1) + 1)
            ax2.axhline(ds.min(), color=YELLOW, ls="--", lw=1.5, alpha=a)
            fig.text(0.67, 0.30, rf"$\min\,\delta^*\approx{ds.min():.3f}>0$", fontsize=22,
                     color=YELLOW, alpha=a)
            fig.text(0.67, 0.22, "→ 通用 δ 存在", fontsize=20, color=GREEN, alpha=a)
            ax2.plot([x0s[j]], [ds[j]], "o", color=YELLOW, ms=8, alpha=a)
        if not first and t > self.k(2) + 4:
            a = ramp(t, self.k(2) + 4, self.k(2) + 5)
            fig.text(0.67, 0.30, r"$\inf\,\delta^*=0$", fontsize=24, color=RED, alpha=a)
            fig.text(0.67, 0.22, "→ 通用 δ 不存在", fontsize=20, color=RED, alpha=a)
        a3 = ramp(t, self.k(3), self.k(3) + 1)
        fig.patches.append(FancyBboxPatch((0.66, 0.50), 0.31, 0.30, boxstyle="round,pad=0.01",
                                          transform=fig.transFigure, facecolor="#1d2330",
                                          edgecolor=YELLOW, lw=1.5, alpha=a3))
        fig.text(0.815, 0.72, "f 在 I 上一致连续", ha="center", fontsize=20, alpha=a3)
        fig.text(0.815, 0.645, r"$\Updownarrow$", ha="center", fontsize=22, alpha=a3)
        fig.text(0.815, 0.555, r"$\forall\varepsilon>0,\ \inf_{x_0\in I}\delta^*(x_0)>0$",
                 ha="center", fontsize=21, alpha=a3, color=YELLOW)


# ---------------------------------------------------------------- 场景 10：一致连续性定理
class S10Cantor(Scene):
    title = "六、一致连续性定理（Cantor 定理）"
    subs = [
        (6.0, "定理：若函数 f 在闭区间 [a,b] 上连续，则 f 在 [a,b] 上一致连续"),
        (6.0, "证明（反证法，借助致密性定理）：假设 f 在 [a,b] 上不一致连续"),
        (6.5, "则存在 ε₀>0，对 δ=1/n 取 x′ₙ, x″ₙ∈[a,b]，|x′ₙ−x″ₙ|<1/n，|f(x′ₙ)−f(x″ₙ)|≥ε₀"),
        (6.5, "{x′ₙ} 有界，由致密性定理，存在收敛子列 x′ₙₖ → x₀，且 x₀∈[a,b]（闭区间！）"),
        (6.0, "由 |x′ₙₖ − x″ₙₖ| < 1/nₖ，得 x″ₙₖ 也收敛到同一点 x₀"),
        (6.5, "f 在 x₀ 连续，故 |f(x′ₙₖ) − f(x″ₙₖ)| → |f(x₀)−f(x₀)| = 0，与 ≥ ε₀ 矛盾 ∎"),
        (6.0, "闭区间不可少：对 (0,1) 上的 1/x，点列会“逃”到区间外的 0，证明失效"),
    ]

    @staticmethod
    def pair(n):
        # 构造一组在 [0,4] 内收敛到 x0=2.5 的示意点对（含一些“杂散”项）
        rng = np.random.default_rng(n)
        if n % 2 == 0:
            base = 2.5 + (1.2 / (n / 2 + 1)) * (-1) ** (n // 2)
        else:
            base = rng.uniform(0.3, 3.7)
        return base, base + 0.7 / n

    def draw(self, fig, t):
        a0 = ramp(t, 0, 1)
        fig.patches.append(FancyBboxPatch((0.07, 0.76), 0.86, 0.11, boxstyle="round,pad=0.01",
                                          transform=fig.transFigure, facecolor="#1d2330",
                                          edgecolor=GREEN, lw=1.5, alpha=a0))
        fig.text(0.5, 0.815, r"$f\in C[a,b]\ \Longrightarrow\ f$ 在 $[a,b]$ 上一致连续",
                 ha="center", va="center", fontsize=26, alpha=a0)

        ax = make_axes(fig, [0.08, 0.38, 0.84, 0.32], (-0.3, 4.3), (-0.4, 1.6))
        ax.set_yticks([])
        ax.spines["left"].set_visible(False)
        g = lambda x: 0.6 + 0.45 * np.sin(1.7 * x) + 0.08 * x
        xs = np.linspace(0, 4, 400)
        last = t >= self.k(6)
        if not last:
            ax.plot(xs, g(xs), color=BLUE, lw=2.2, alpha=0.8)
            ax.plot([0, 4], [-0.2, -0.2], color=FG, lw=3)
            ax.plot([0, 4], [-0.2, -0.2], "|", color=FG, ms=18, mew=3)
            ax.text(0, -0.38, "a", ha="center", fontsize=17, va="top")
            ax.text(4, -0.38, "b", ha="center", fontsize=17, va="top")
            ax.set_xticks([])
        x0 = 2.5
        if self.k(2) <= t < self.k(6):
            N = 2 + int(22 * ramp(t, self.k(2), self.k(3)))
            sub = t >= self.k(3)
            for n in range(1, N + 1):
                x1, x2 = self.pair(n)
                in_sub = n % 2 == 0
                if sub and not in_sub:
                    al = 0.12
                else:
                    al = 0.35 + 0.65 * (n == N or (sub and n == (N // 2) * 2))
                show2 = t >= self.k(4) or not sub
                ax.plot([x1], [-0.2], "o", color=ORANGE, ms=6, alpha=al, zorder=6)
                if show2:
                    ax.plot([x2], [-0.2], "o", color=PURPLE, ms=6, alpha=al, zorder=6)
                if (not sub) or (in_sub and t >= self.k(5)):
                    ax.plot([x1, x1], [-0.2, g(x1)], color=ORANGE, lw=0.6, alpha=al * 0.6)
                    ax.plot([x2, x2], [-0.2, g(x2)], color=PURPLE, lw=0.6, alpha=al * 0.6)
            if t >= self.k(3):
                a = ramp(t, self.k(3), self.k(3) + 1)
                ax.plot([x0], [-0.2], "*", color=YELLOW, ms=18, alpha=a, zorder=9)
                ax.text(x0, 0.0, r"$x_0$", color=YELLOW, fontsize=18, ha="center", alpha=a)
            fig.text(0.08, 0.30, r"$\bullet\ x_n'$", color=ORANGE, fontsize=18)
            fig.text(0.16, 0.30, r"$\bullet\ x_n''$", color=PURPLE, fontsize=18)
        if t >= self.k(5) and not last:
            a = ramp(t, self.k(5), self.k(5) + 1)
            ax.plot([x0], [g(x0)], "o", color=YELLOW, ms=9, alpha=a)
            ax.text(x0 + 0.1, g(x0) + 0.12, r"$f(x_0)$", color=YELLOW, fontsize=17, alpha=a)
        if last:
            f = lambda x: 1 / x
            ax.set_xlim(-0.05, 1.05)
            ax.set_ylim(0, 12)
            ax.set_xticks([0, 1])
            xs = np.linspace(0.09, 1, 300)
            ax.plot(xs, f(xs), color=BLUE, lw=2.2)
            ax.plot([0], [0.4], "o", mfc=BG, mec=RED, ms=10, mew=2, zorder=9)
            for n in range(2, 12):
                ax.plot([1 / n], [0.4], "o", color=ORANGE, ms=6, alpha=min(1, 0.4 + 0.05 * n))
            ax.annotate("极限点 0 ∉ (0,1)", xy=(0.01, 0.6), xytext=(0.25, 6), fontsize=17,
                        color=RED, arrowprops=dict(arrowstyle="->", color=RED))
        # 文字推导
        lines = [
            (1, r"假设不一致连续 $\Rightarrow\ \exists\varepsilon_0>0$"),
            (2, r"$|x_n'-x_n''|<\frac{1}{n},\ |f(x_n')-f(x_n'')|\geq\varepsilon_0$"),
            (3, r"致密性定理：$x_{n_k}'\to x_0\in[a,b]$"),
            (4, r"$x_{n_k}''\to x_0$"),
            (5, r"$\varepsilon_0\leq|f(x_{n_k}')-f(x_{n_k}'')|\to 0$，矛盾"),
        ]
        if not last:
            for j, (i, s) in enumerate(lines):
                a = ramp(t, self.k(i), self.k(i) + 0.8)
                col = RED if i == 5 else FG
                fig.text(0.30 + (j % 2) * 0.33, 0.30 - (j // 2) * 0.065, s, fontsize=17,
                         alpha=a, color=col)


# ---------------------------------------------------------------- 场景 11：小结
class S11Summary(Scene):
    title = "小结"
    subs = [
        (6.0, "1. 一致连续要求 δ 只依赖于 ε，与点的位置无关；它是函数在整个区间上的性质"),
        (6.0, "2. 证明一致连续：估计 |f(x′)−f(x″)| ≤ L|x′−x″| 等，直接找出 δ(ε)"),
        (6.0, "3. 证明不一致连续：构造 x′ₙ−x″ₙ→0 但 |f(x′ₙ)−f(x″ₙ)|≥ε₀ 的两列点"),
        (6.0, "4. Cantor 定理：闭区间上的连续函数必一致连续"),
        (7.0, "思考题：证明 f 在 (a,b) 上一致连续 ⇔ f 在 (a,b) 上连续，且 f(a+0)、f(b−0) 都存在"),
    ]

    def draw(self, fig, t):
        items = [
            (YELLOW, "一致连续：", r"$\delta=\delta(\varepsilon)$，与 $x$ 无关 —— 整体性质"),
            (GREEN, "证明一致连续：", r"$|f(x')-f(x'')|\leq L|x'-x''|\ \Rightarrow\ "
                                   r"\delta=\varepsilon/L$"),
            (RED, "证明不一致连续：", r"$x_n'-x_n''\to0,\ |f(x_n')-f(x_n'')|\geq\varepsilon_0$"),
            (BLUE, "Cantor 定理：", r"$f\in C[a,b]\ \Rightarrow$ 一致连续"),
        ]
        for i, (c, h, s) in enumerate(items):
            a = ramp(t, self.k(i), self.k(i) + 0.8)
            y = 0.76 - i * 0.11
            fig.text(0.08, y, h, fontsize=23, color=c, alpha=a)
            fig.text(0.33, y, s, fontsize=23, alpha=a)
        a = ramp(t, self.k(4), self.k(4) + 0.8)
        fig.patches.append(FancyBboxPatch((0.07, 0.17), 0.86, 0.15, boxstyle="round,pad=0.01",
                                          transform=fig.transFigure, facecolor="#1d2330",
                                          edgecolor=PURPLE, lw=1.5, alpha=a))
        fig.text(0.09, 0.27, "思考题", fontsize=19, color=PURPLE, alpha=a)
        fig.text(0.5, 0.215,
                 r"$f$ 在 $(a,b)$ 上一致连续 $\Longleftrightarrow$ $f\in C(a,b)$ 且 "
                 r"$f(a+0),\ f(b-0)$ 存在", ha="center", fontsize=22, alpha=a)


SCENES = [S01Title, S02Review, S03Definition, S04Geometry, S05Sin, S06Negation,
          S07Reciprocal, S08MoreCounter, S09DeltaCurve, S10Cantor, S11Summary]


# ---------------------------------------------------------------- 渲染
def render_scene(args):
    idx, fps, outdir = args
    scene = SCENES[idx]()
    path = os.path.join(outdir, f"scene_{idx:02d}.mp4")
    n = scene_frames(scene, fps)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "medium",
           "-crf", "20", "-pix_fmt", "yuv420p", path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(n):
        proc.stdin.write(scene.frame(i / fps).tobytes())
    proc.stdin.close()
    proc.wait()
    print(f"  scene {idx + 1:2d}/{len(SCENES)} done ({scene.duration:.1f}s)", flush=True)
    return path


def scene_frames(scene, fps):
    return int(round(scene.duration * fps))


def build_audio(idxs, fps, path, sr=24000):
    """按各字幕段的起始时刻拼接配音，返回是否生成了音轨"""
    import soundfile as sf

    if not AUDIO_DURATIONS:
        return False
    scenes = [SCENES[i]() for i in idxs]
    total = sum(scene_frames(sc, fps) for sc in scenes) / fps
    track = np.zeros(int(total * sr) + sr, dtype=np.float32)
    base = 0.0
    for sc in scenes:
        name = type(sc).__name__
        for j, st in enumerate(sc.starts):
            wav = os.path.join(AUDIO_DIR, f"{name}_{j}.wav")
            if not os.path.exists(wav):
                continue
            data, rate = sf.read(wav, dtype="float32")
            assert rate == sr, wav
            i0 = int((base + st + LEAD) * sr)
            track[i0:i0 + len(data)] += data
        base += scene_frames(sc, fps) / fps
    peak = np.abs(track).max()
    if peak > 0:
        track *= 0.89 / peak
    sf.write(path, track[:int(total * sr)], sr)
    return True


def write_srt(path):
    def ts(x):
        ms = int(round(x * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    lines, k, base = [], 1, 0.0
    for S in SCENES:
        s = S()
        for (d, text), st in zip(s.segs, s.starts):
            lines.append(f"{k}\n{ts(base + st)} --> {ts(base + st + d)}\n{text}\n")
            k += 1
        base += s.duration
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="uniform_continuity.mp4")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--scene", type=int, help="只渲染第 N 个场景（1 起）")
    ap.add_argument("--preview", action="store_true", help="导出每个字幕段中间时刻的 PNG")
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    a = ap.parse_args()

    idxs = [a.scene - 1] if a.scene else list(range(len(SCENES)))
    if a.preview:
        os.makedirs("preview", exist_ok=True)
        for i in idxs:
            s = SCENES[i]()
            for j, (d, _) in enumerate(s.segs):
                img = s.frame(s.starts[j] + d * 0.8)
                plt.imsave(f"preview/s{i + 1:02d}_{j + 1}.png", img)
        print("previews written to preview/")
        return

    total = sum(SCENES[i]().duration for i in idxs)
    print(f"rendering {len(idxs)} scenes, {total:.0f}s @ {a.fps}fps")
    with tempfile.TemporaryDirectory() as tmp:
        with Pool(a.jobs) as pool:
            paths = pool.map(render_scene, [(i, a.fps, tmp) for i in idxs])
        lst = os.path.join(tmp, "list.txt")
        with open(lst, "w") as fh:
            fh.writelines(f"file '{p}'\n" for p in paths)
        silent = os.path.join(tmp, "video.mp4")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", lst, "-c", "copy", silent], check=True)
        voice = os.path.join(tmp, "voice.wav")
        if build_audio(idxs, a.fps, voice):
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", silent, "-i", voice,
                            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                            "-movflags", "+faststart", a.out], check=True)
        else:
            print("audio/durations.json 不存在，输出无配音版本")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", silent, "-c", "copy",
                            "-movflags", "+faststart", a.out], check=True)
    if not a.scene:
        write_srt(os.path.splitext(a.out)[0] + ".srt")
    print("wrote", a.out)


if __name__ == "__main__":
    sys.exit(main())
