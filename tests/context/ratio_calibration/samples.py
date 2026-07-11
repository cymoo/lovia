"""Deterministic conversation samples for the ratio-calibration study.

Each scenario grows a homogeneous transcript one user/assistant pair per
turn. Content is generated from fixed sentence pools by index rotation, so a
run is byte-for-byte reproducible (no live model output feeds back in) and
every turn adds a comparable amount of the scenario's content type. The point
of keeping the *type* constant across a scenario is that the estimator's
systematic error — the thing :data:`lovia.context.state.CompactionState.ratio`
learns — is a property of the content's script mix, so a scenario must not
dilute it by mixing types turn to turn.

Five scenarios, matching the request:

* ``en``      — English technical prose (BPE-friendly, whitespace-delimited).
* ``zh``      — Chinese technical prose (3-byte characters, dense information).
* ``zh_en``   — Chinese prose interleaved with English technical terms.
* ``zh_code`` — Chinese explanation wrapped around real Python code blocks.
* ``en_code`` — English explanation wrapped around real Python code blocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# --------------------------------------------------------------------------- #
# Sentence / snippet pools. Rotated by turn index so each turn draws a
# different-but-similar slice; wrapping is fine — a real conversation revisits
# the same concepts, and repetition only exercises the prompt cache, not the
# ratio (identical bytes tokenize identically).
# --------------------------------------------------------------------------- #

_EN = [
    "The compaction pipeline treats the transcript as immutable and renders a "
    "per-call view instead of editing history in place.",
    "Sticky decisions are monotonic: a cleared tool result never reverts, and "
    "summary coverage only ever grows forward.",
    "Keeping the rendered prefix byte-stable is what lets a provider's prompt "
    "cache stay warm from one turn to the next.",
    "Between compaction bursts nothing is touched, and that gap is the "
    "hysteresis that stops the policy from thrashing every single turn.",
    "Token thresholds estimate the whole request, cheap per-entry counts plus "
    "the fixed tool-schema payload the call carries alongside the entries.",
    "The calibration ratio narrows its job to tokenizer error, which is "
    "genuinely multiplicative, so it stays valid as the transcript grows.",
    "An overflow proves the effective limit is at most the failed prompt, so "
    "the reactive path budgets against the actual prompt size.",
    "Offload archives a large result to a durable store and leaves a compact "
    "preview marker in its place, recoverable through a recall tool.",
    "The protected tail is measured on rendered entries so cleared results "
    "cost marker size, not the phantom tokens of their original output.",
    "A learned window outranks a tabled one because the number in a rejection "
    "is the limit being enforced, not merely a claim about the deployment.",
    "Estimates are deliberately rough; the pipeline corrects them against the "
    "provider's real input-token counts from the previous call.",
    "Counting the schemas separately keeps that fixed additive payload out of "
    "the multiplier, whose only remaining job is pure tokenizer residual.",
    "The two watermarks accept either a fraction of the usable window or an "
    "absolute token count, and the gap between them is anti-thrash headroom.",
    "Per-turn injected view entries stay unmodeled by contract, small enough "
    "that the calibration ratio simply absorbs them along with request framing.",
]

_EN_Q = [
    "Walk me through how the sticky view stays byte-stable across turns.",
    "Why is the calibration ratio multiplicative rather than additive?",
    "What happens on the reactive path after a real provider overflow?",
    "How does offload differ from clearing a tool result?",
    "Explain why the tool schemas are counted separately from the entries.",
    "How is the protected recent tail measured, and why that way?",
    "When does a learned context window override the bundled table?",
]

_ZH = [
    "上下文压缩把转录视为不可变对象，每次调用都重新渲染一份视图，而不是就地"
    "改写历史记录。",
    "粘性决策是单调的：被清除的工具结果永不回退，摘要的覆盖范围也只会向前推进。",
    "保持渲染出的前缀在字节层面稳定，正是供应商的提示缓存能够跨轮次持续命中的前提。",
    "在两次压缩之间不触碰任何内容，这段间隔构成了滞后带，避免策略在每一轮都发生抖动。",
    "令牌阈值估算的是整个请求，既包含逐条目的廉价估计，也包含请求随附的固定工具"
    "模式负载。",
    "校准比率把自己的职责收窄到纯粹的分词器误差，这种误差本质上是乘性的，因此随着"
    "转录增长依然有效。",
    "一次溢出证明有效上限至多等于失败的那次提示，因此反应式路径按提示的实际大小"
    "来编制预算。",
    "卸载会把体量庞大的结果归档到持久存储，并在原处留下一个紧凑的预览标记，可通过"
    "召回工具恢复。",
    "受保护的尾部是在渲染后的条目上测量的，被清除的结果只按标记大小计费，而非其"
    "原始输出的幽灵令牌。",
    "习得的窗口优先级高于表格中的窗口，因为拒绝响应里的数字是正在强制执行的上限，"
    "而不仅仅是一种声明。",
    "估计值刻意保持粗糙，流水线会用上一次调用中供应商的真实输入令牌数来加以修正。",
    "单独统计模式负载，能把这份固定的加性开销从乘子中剥离，使乘子只保留纯粹的"
    "分词残差。",
    "两条水位线既可以接受可用窗口的一个比例，也可以接受一个绝对令牌数，二者之间的"
    "空隙就是防抖余量。",
    "每轮注入的视图条目按约定不予建模，它们足够小，校准比率会连同请求框架一起把"
    "它们吸收掉。",
]

_ZH_Q = [
    "请讲讲粘性视图是如何跨轮次保持字节稳定的。",
    "为什么校准比率是乘性的而不是加性的？",
    "在真实的供应商溢出之后，反应式路径会发生什么？",
    "卸载和清除工具结果之间有什么区别？",
    "解释一下为什么工具模式要和条目分开统计。",
    "受保护的近期尾部是怎么测量的，为什么要这样做？",
    "习得的上下文窗口什么时候会覆盖内置表格？",
]

# Genuinely interleaved: Chinese matrix carrying English technical terms.
_ZH_EN = [
    "在 lovia 的 runtime 里，preflight 负责串行门控：cancel check、budget、"
    "tool lookup 以及 approval 流程都在这里收敛。",
    "每一次 rejection 都恰好追加一个 ToolResultEntry，因此 tool call 不会悬空，"
    "整个 run 也不会挂起。",
    "sticky 决策一旦写入 scratch 就随 checkpoint 往返，next run 在同一 session 上"
    "继承它，无需重新推导。",
    "estimator 用 UTF-8 byte length 做算术，CJK 大约折算成 0.75 token/char，"
    "避免了 naive chars/4 的四倍 under-count。",
    "calibration 是一条 clamped EMA：alpha 取 0.2，RATIO_MIN 与 RATIO_MAX 把一次"
    "异常的 usage report 挡在门外。",
    "OffloadToolResults 先把 large result 写进 store，再用 preview marker 占位，"
    "recall_tool_result 负责把它取回来。",
    "provider 的 overflow 会带回 reported_window，我们把它记进 learned_windows，"
    "从此这个 endpoint 就按正确的 window 编预算。",
    "render_view 是一个 pure function：immutable transcript 加上 sticky state，"
    "确定性地重建出 per-call 的视图。",
    "watermark 支持 fraction 或绝对 token count，compact_at 与 compact_to 之间的"
    "gap 就是 anti-thrash 的 hysteresis。",
    "TokenCounter 用 id() 做 memo，并配一个 weakref liveness guard，长 transcript"
    "因此每轮只按 new entries 重新计数。",
    "summary 的 fingerprint 一旦对不上，说明 covered prefix 被 rewrite 过，"
    "于是我们 drop 掉它并回退到最近的 pair-safe cut。",
    "reserve_output_tokens 从 usable window 里预留出 headroom，给 model 的 reply"
    "留下足够的 output 空间。",
    "aggressive 路径会把 target 收得更紧，让 summarize 这类真正发起 model call 的"
    "stage 一定有空间去 shrink。",
    "tool schema 是每个 request 的 fixed additive payload，把它单独 count 出来，"
    "ratio 才能专注于 tokenizer error。",
]

_ZH_EN_Q = [
    "preflight 的这些 gate 是按什么顺序 short-circuit 的？",
    "clamped EMA 里的 alpha 和 RATIO_MIN/MAX 各自防的是什么？",
    "offload 之后 recall_tool_result 是怎么把结果取回来的？",
    "learned_windows 相比 tabled window 的优先级是怎么定的？",
    "render_view 为什么要写成一个 pure function？",
    "为什么 tool schema 要从 multiplier 里剥离出来单独 count？",
    "summary 的 fingerprint mismatch 会触发怎样的 rewind？",
]

# Real Python snippets drawn from the compaction subsystem's shape.
_CODE = [
    "def pressure(self, tokens: int) -> float:\n    return tokens / self.usable",
    "def usable_tokens(window: int, reserve_output: int) -> int:\n"
    "    if reserve_output >= window:\n"
    "        return max(window // 2, 1)\n"
    "    return window - reserve_output",
    "observed = req.last_input_tokens / max(1, state.last_view_estimate)\n"
    "state.ratio = min(RATIO_MAX, max(RATIO_MIN,\n"
    "    (1 - alpha) * state.ratio + alpha * observed))",
    "tokens = int((raw + overhead) * state.ratio)\n"
    "if not aggressive and tokens < budget.trigger_tokens:\n"
    "    return self._result(req, state, view, raw + overhead, tokens)",
    "candidates = [w for w in (learned, advertised) if w is not None]\n"
    "return min(candidates) if candidates else None",
    "def count(self, entries: Sequence[TranscriptEntry]) -> int:\n"
    "    return sum(self.count_entry(entry) for entry in entries)",
    "for stage in self.stages:\n"
    "    if await stage.plan(body, ctx):\n"
    "        reasons.append(stage.name)\n"
    "    if tokens <= budget.target_tokens:\n"
    "        break",
    "size = len(s.encode('utf-8', 'surrogatepass'))\n"
    "return size // _BYTES_PER_TOKEN + self.entry_overhead",
]

_EN_CODE_LEAD = [
    "Pressure is just the estimated fill of the usable window:",
    "The usable-window helper degrades gracefully on tiny local models:",
    "Calibration folds the last real count into a clamped EMA:",
    "The early-return keeps the prompt prefix byte-stable below the watermark:",
    "Window resolution prefers the smallest credible number:",
    "Counting a view is a memoized sum over its entries:",
    "The stage loop stops as soon as the target is met:",
    "The byte-weighted estimate is one encode per entry, then divided:",
]

_EN_CODE_TAIL = [
    "Because the estimate is uncalibrated here, the ratio multiplies it "
    "afterward to land near the provider's real count.",
    "Note the branch never returns zero — a token budget of one still leaves "
    "the stages something to work against.",
    "The clamp bounds how hard a single bad usage report can move the scale "
    "in either direction.",
    "Nothing before the watermark is mutated, which is exactly what keeps the "
    "cache warm across turns.",
    "A learned window can only ever cap the advertised one, never raise it.",
    "The memo is keyed by identity with a weakref guard so id reuse after GC "
    "cannot hand back a stale count.",
    "Hysteresis lives in the gap between trigger and target, so bursts are "
    "rare instead of per-turn.",
    "Multi-byte scripts weigh in proportionally, so CJK is not under-counted "
    "four-fold the way a plain char count would.",
]

_ZH_CODE_LEAD = [
    "压力就是估算令牌占可用窗口的比例：",
    "可用窗口的辅助函数在极小的本地模型上也能优雅退化：",
    "校准把上一次的真实计数折进一条带钳位的 EMA：",
    "水位线以下提前返回，可让提示前缀保持字节稳定：",
    "窗口解析总是优先选取最小的可信数字：",
    "对视图计数就是在其条目上做一次带记忆化的求和：",
    "阶段循环一旦达到目标就立即停止：",
    "按字节加权的估计对每个条目只做一次编码，然后整除：",
]

_ZH_CODE_TAIL = [
    "由于这里的估计尚未校准，比率会随后乘上去，使结果贴近供应商的真实计数。",
    "注意这个分支永远不会返回零——哪怕令牌预算只有一，也要给各阶段留下可压缩的余地。",
    "钳位限定了单次异常的用量报告能在两个方向上把标度推动多远。",
    "水位线之前的一切都不被改写，这正是缓存能跨轮次保持温热的原因。",
    "习得的窗口只能向下钳住广告窗口，绝不会把它抬高。",
    "记忆表以身份为键并配以弱引用守卫，使得 GC 后的 id 复用不会返回过期的计数。",
    "滞后存在于触发与目标之间的间隙里，于是压缩成为罕见的突发而非每轮发生。",
    "多字节文字按比例计入，因此中文不会像朴素字符计数那样被四倍地低估。",
]


def _rotate(pool: list[str], turn: int, count: int) -> list[str]:
    """Pick ``count`` consecutive items starting at a turn-dependent offset."""
    start = (turn * count) % len(pool)
    return [pool[(start + k) % len(pool)] for k in range(count)]


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    blurb: str
    question: Callable[[int], str]
    answer: Callable[[int], str]


def _prose(pool: list[str], qpool: list[str], sentences: int):
    def question(turn: int) -> str:
        return qpool[turn % len(qpool)]

    def answer(turn: int) -> str:
        return " ".join(_rotate(pool, turn, sentences))

    return question, answer


def _code(lead: list[str], tail: list[str], qpool: list[str], joiner: str):
    def question(turn: int) -> str:
        return qpool[turn % len(qpool)]

    def answer(turn: int) -> str:
        snippet = _CODE[turn % len(_CODE)]
        return (
            f"{lead[turn % len(lead)]}{joiner}"
            f"```python\n{snippet}\n```{joiner}"
            f"{tail[turn % len(tail)]}"
        )

    return question, answer


_en_q, _en_a = _prose(_EN, _EN_Q, 4)
_zh_q, _zh_a = _prose(_ZH, _ZH_Q, 4)
_zhen_q, _zhen_a = _prose(_ZH_EN, _ZH_EN_Q, 4)
_encode_q, _encode_a = _code(_EN_CODE_LEAD, _EN_CODE_TAIL, _EN_Q, "\n\n")
_zhcode_q, _zhcode_a = _code(_ZH_CODE_LEAD, _ZH_CODE_TAIL, _ZH_Q, "\n\n")


SCENARIOS: list[Scenario] = [
    Scenario(
        "en",
        "English prose",
        "Whitespace-delimited English technical writing — the BPE tokenizer's "
        "home turf, where byte/4 tends to over-count.",
        _en_q,
        _en_a,
    ),
    Scenario(
        "zh",
        "Chinese prose",
        "Dense Chinese technical writing — 3-byte characters the byte-weighted "
        "heuristic prices at ~0.75 tokens/char.",
        _zh_q,
        _zh_a,
    ),
    Scenario(
        "zh_en",
        "Chinese + English mixed",
        "A Chinese matrix carrying English technical terms in every sentence — "
        "two scripts, two tokenizer regimes, one stream.",
        _zhen_q,
        _zhen_a,
    ),
    Scenario(
        "zh_code",
        "Chinese + code mixed",
        "Chinese explanation wrapped around real Python code blocks — prose "
        "CJK against symbol-dense, BPE-friendly source.",
        _zhcode_q,
        _zhcode_a,
    ),
    Scenario(
        "en_code",
        "English + code mixed",
        "English explanation wrapped around real Python code blocks — symbols "
        "and newlines pull the estimate back toward byte/4, unlike pure prose.",
        _encode_q,
        _encode_a,
    ),
]


__all__ = ["Scenario", "SCENARIOS"]
