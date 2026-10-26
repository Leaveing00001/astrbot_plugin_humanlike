"""astrbot_plugin_humanlike —— 给任意人格加一层"人味"。

目标不是让机器人更聪明，而是让它更像群里的一个真人：
它会有反应时间、会有心情、会把话说短、会在生气时话少。

五层机制（按一次回复的先后顺序）：

    记账层  note_incoming()        只记事：把人话记下来、按内容推情绪。不裁决回不回
    提示层  shape_request()        把「怎么说」和「什么时候该引用/艾特」写进提示词
    形态层  on_decorating_result() 清洗 Markdown / 括号动作 / 客套腔，压强短句
    节奏层  _delivery_pause()      完整回复到手之后才决定：秒回，还是停一下再发
    兜底层  _is_error_result()     LLM 报错只写日志，绝不发进群里

「这条要不要回」「要不要主动接话」全部交回上游（AstrBot 自己、Proactive Chat、
angel_heart 等）决定——本插件不再看门、不再插话、也不再抢话，只在上游已经
生成出完整回复之后动手，管它「发多久、说多短、什么语气」。

人格由 AstrBot 的人设（或系统提示词）提供，本插件只改「怎么说」，不改「是谁」。
**插件不假设人格叫什么**：唯一用到名字的地方是「隐形模式」把自称换成「我」，
名字取自 `persona_name`，留空就自动读当前会话的人设名。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger, message_components as Comp
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools

try:  # 插件 Web API：给面板页面用，老版本 AstrBot 没有这个模块
    from astrbot.api.web import error_response, json_response
    from astrbot.api.web import request as web_request

    _WEB_API = True
except Exception:  # pragma: no cover - 兼容老版本
    error_response = json_response = None  # type: ignore[assignment]
    web_request = None  # type: ignore[assignment]
    _WEB_API = False

PLUGIN_NAME = "astrbot_plugin_humanlike"
_PLUGIN_VERSION = "0.9.2"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"

# ────────────────────────── 文本清洗 ──────────────────────────

_RE_CODE = re.compile(r"`{1,3}([^`]*)`{1,3}")
_RE_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_RE_UNDER = re.compile(r"__(.+?)__")
_RE_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_RE_HEAD = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_RE_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.、)])\s+", re.MULTILINE)
_RE_QUOTE = re.compile(r"^\s*>\s?", re.MULTILINE)
_RE_ACTION = re.compile(r"[（(][^（）()\n]{0,24}[）)]")
_RE_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\u2B00-\u2BFF]"
)
_RE_INLINE_SPACE = re.compile(r"[ \t\u3000]+")
_RE_BLANK = re.compile(r"\s*\n+\s*")
_RE_TRAILING_DOT = re.compile(r"[。.]+$")
_RE_SENT_SPLIT = re.compile(r"(?<=[。！？!?])")
# 模型自己输出的艾特标记，交给 QQ 工具箱（enable_auto_at_conversion）转成真实 At。
# 先占位再还原，免得截断/清洗把 "[At:123456]" 切成半截发出去。
_RE_AT_MARK = re.compile(r"\[At:(\d{4,12})\]")
_RE_AT_TOKEN = re.compile(r"@@AT(\d{1,3})@@")
_RE_AT_LEAD = re.compile(r"^(\[At:\d{4,12}\][\s\u200b]*)")
_RE_AT_TRUNC = re.compile(r"\[At:?[^\]]*$")

#: 口癖替换：这些字符算分句边界
_CLAUSE_BREAKS = "，。！？…～、,.!?~\n "

#: 句首口癖（「喵，我才不笨」）换成这几个感叹词，比「呀」自然
_TIC_LEAD = ("唔", "哦", "诶", "嗯", "啊")

# 上游出错时 AstrBot 会把错误文本当回复发出来。下面这些开头一出现，
# 就说明这条不是这个角色说的话，而是给运维看的——拦下来写日志，别让群友看见。
_ERROR_HEADS = (
    "LLM 响应错误",
    "LLM 请求失败",
    "All chat models failed",
    "Error occurred during AI execution",
    "Error occurred while processing agent request",
)
# 只有同时出现这两段，才算「模型接口报了 HTTP 错」，避免误杀正常聊到报错的对话
_ERROR_BODY = ("Error code:", "'error'")

# 历史里的图片内容块能被模型接受的 url 形态
_OK_IMAGE_PREFIX = ("data:", "http://", "https://")

# 句首的连接词/助手腔开场，只削掉这几个字，不连坐整句
_RE_AI_LEAD = re.compile(
    r"^\s*(?:首先|其次|另外|然后|总之|总的来说|总而言之|总结一下|需要注意的是|值得一提的是)"
    r"[，,：:\s]*"
)

# 助手腔：命中就整句丢掉
_AI_PHRASES = (
    "作为一个ai",
    "作为ai",
    "我是ai",
    "作为一个语言模型",
    "人工智能助手",
    "希望这对你有帮助",
    "希望以上",
    "如果你还有其他",
    "如果还有其他",
    "需要注意的是",
    "值得一提的是",
    "总的来说",
    "总而言之",
    "总结一下",
    "有什么可以帮",
    "随时告诉我",
)

# 情境关键词 → 给模型的一句额外指令
_SITUATIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        # 只认"真的是叫你去打游戏/出门"的说法。
        # 早先这里是 ("来不来", "一起", "来吗", ...) —— 范围太宽：
        # 「合创 OC 你们来吗」也被判成开黑邀请，模型于是被硬性要求"拒绝，别答应"，
        # 结果连着两条都在推辞，看着比答应还假。改成自己定，只保留"别每次都答应"的约束。
        ("开黑", "上号", "组队", "打游戏", "来打", "联机", "面基", "出来玩", "打球"),
        "对方在叫你一起出门或打游戏。去不去你自己定：想去就答应，不想去就含糊推掉（有事/懒得动），别每次都答应，也别每次都说有事。",
    ),
    (
        ("在吗", "在不在", "在么", "人呢"),
        "对方在确认你在不在。回一个很短的词就行，不用解释为什么在。",
    ),
    (
        ("催", "怎么还不", "快点", "回我", "说话"),
        "对方在催你。句子更短，可以带一点点不耐烦，但别吵架。",
    ),
    (
        ("厉害", "可爱", "好看", "喜欢你", "谢谢", "辛苦"),
        "对方在夸你或道谢。别客套地谦虚，接一句就好。",
    ),
    (
        ("是不是人", "是不是机器人", "ai", "AI", "机器人吧"),
        "对方在试探你是不是真人。别否认得太用力，也别承认，用一句敷衍或反问带过去。",
    ),
)

# ────────────────────── 反露馅：模式抽取 ──────────────────────
#
# 判官在盲判时反复抓到的四类破绽，都不是"语气"问题，而是"关系"问题：
#   ① 同一句约束说两遍（别太辣 / 别放太多辣）
#   ② 把别人的属性接到自己身上（乙：反正我开车 → 丁：我开车就不喝了）
#   ③ 别人已经说定的事当没发生（乙：订好位了 → 丁：先占个位）
#   ④ 话题已经翻篇了还回头补答
# 下面这几组正则就是给这四类做结构化检查用的，不依赖模型自觉。

# 约束标记：不管用哪种说法（别太辣 / 别放太多辣 / 微辣），
# 抽出来的都是"被约束的那个对象"，这样两种措辞会落到同一个键上。
_RE_CONSTRAINT = re.compile(r"(别|甭|少|不要|微|重|超|太)([\u4e00-\u9fff]{1,8})")
# 约束词后面跟的动词、程度词、助词，剥掉之后剩下的才是被约束的对象
_CONSTRAINT_LEAD = (
    "点", "放", "加", "弄", "整", "带", "喝", "吃", "要", "买", "来", "做", "叫",
    "那么", "这么", "太", "多", "少", "些", "的", "了", "得", "地",
)

# 功能字：取实词时遇到就停
_FUNC_CHARS = set("就都是也还又才再太多少点些吧了呢啊呀吗么的得地个只把被给对从向在和与或而且然后所以但是")

# 第一人称属性抽取（"我开车"→开车）
_RE_SELF_CLAIM = re.compile(r"我([\u4e00-\u9fff]{1,6})")
_CLAIM_LEAD = (
    "反正", "平时", "一般", "今天", "明天", "昨天", "已经", "本来", "其实",
    "就", "也", "还", "都", "才", "先", "再", "又",
)
# 这些不是"属性"，是任何人都会说的泛用自我表述，不参与错配比对
_CLAIM_BLACKLIST = {
    "无所", "不知", "觉得", "可以", "不行", "没空", "有空", "随便", "不确",
    "说不", "想要", "应该", "可能", "感觉", "记得", "先走", "说到", "应该",
}

# 【群里已经说定的事】↔【与之冲突的做法】
_FACT_CONFLICTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("订好位", "订到位", "定了位", "订了位", "定好位", "位置订好"),
        ("占位", "占个位", "先占", "排队", "再订", "订个位", "订位"),
    ),
    (
        ("票买好", "买好票", "票买了", "买了票"),
        ("买票", "订票", "抢票", "谁买票"),
    ),
)

# 【起哄 / 拆台】的强信号：出现任意一条就认为这一轮在挑 bot
_TEASE_STRONG = (
    "笑死", "打脸", "你自己说", "不是说不", "他上次不是说", "你不是说",
    "说不吃还", "说不去还", "说不喝还", "翻旧账", "还好意思", "记性真好",
)
# 被指出"你之前明明说过"——这种要否认，不能只用语气词糊过去
_TEASE_DENY = (
    "你自己说", "不是说不", "他上次不是说", "你不是说", "说不吃还",
    "说不去还", "说不喝还", "翻旧账", "打脸",
)
_TEASE_WEAK = ("哈哈", "服了", "离谱", "无语", "咋又", "又来了")

# 被挑的时候可以拿来接话的反应，按"挑的哪种"分两组；组内按 LRU 取，避免连着用同一句。
# 全部都能单独发一条，因为实际用法就是"答完之后再补一句"。
_REACT_DENY = (  # 被指出"你自己说过 / 上次不是说"——该否认
    "我什么时候说过了",
    "冤枉",
    "记性这么好干嘛",
    "谁记这么清楚",
    "别翻旧账",
)
_REACT_LAUGH = (  # 只是被起哄——可以不接，也可以损回去
    "行行 你说得对",
    "有那么好笑吗",
    "我认栽",
    "我改主意了不行",
    "啊这",
)
# 回复里已经有这些，就说明已经给过反应（或已经推回去了），不再补
_REACTION_MARKS = (
    "笑死", "哈哈", "我认", "认栽", "冤枉", "记性", "翻旧账", "改主意",
    "谁记", "你说得对", "啊这", "行行", "有那么好笑",
    "咋", "怎么", "凭什么", "什么时候", "哪有", "没说过", "别乱", "哪只",
)

# 比较"这句在回答哪个话题"时忽略的字
_TOPIC_STOP = set(
    "我你他她它的了是不就还也都有一个上下这那和吧啊呢们要会能说去到点太多少好大小没"
    "在过又才被把给对从向与或而且然后所以但是什么怎么这个那个"
)

# 纯附和（嗯/好/笑死/算）不再需要单独判据：回不回已经交回上游，
# 这里只保留「被起哄」的识别表。


def _strip_lead(s: str, words: tuple[str, ...]) -> str:
    """反复剥掉开头的填充词。"""
    changed = True
    while changed and s:
        changed = False
        for w in words:
            if s.startswith(w):
                s = s[len(w):]
                changed = True
                break
    return s


def _noun_after(s: str) -> str:
    """从一串汉字里取最前面的实词（1~2 字，遇到功能字就停）。"""
    out = ""
    for ch in s:
        if ch in _FUNC_CHARS:
            break
        out += ch
        if len(out) >= 2:
            break
    return out


def _stance_keys(text: str) -> set[str]:
    """抽取「别太辣 / 别放太多辣 / 微辣」这类可被复读的约束，统一成被约束的对象。"""
    keys: set[str] = set()
    for m in _RE_CONSTRAINT.finditer(text or ""):
        tail = _strip_lead(m.group(2), _CONSTRAINT_LEAD)
        noun = _noun_after(tail)
        if noun:
            keys.add(noun)
    return keys


def _self_claims(text: str) -> set[str]:
    """抽取「我开车」「我加班」这类第一人称属性。"""
    claims: set[str] = set()
    for m in _RE_SELF_CLAIM.finditer(text or ""):
        tail = _strip_lead(m.group(1), _CLAIM_LEAD)
        noun = _noun_after(tail)
        if len(noun) < 2 or noun in _CLAIM_BLACKLIST:
            continue
        # 时间/数量不是"身份属性"，谁都可以说自己几点到
        if noun[0] in "零〇一二三四五六七八九十百两半几0123456789":
            continue
        claims.add(noun)
    return claims


def _topic_set(text: str) -> set[str]:
    """话题字集合：只留实字，用来判断"这句在回答哪个话题"。"""
    return {ch for ch in (text or "") if "\u4e00" <= ch <= "\u9fff" and ch not in _TOPIC_STOP}


# ────────────────── 情绪：三条轴，不是一条"心情" ──────────────────
#
# 早先只有一个 mood 标量，所有提示都落在"句子短一点 / 敷衍一点"上，于是不管
# 发生什么，读起来都是同一个懒散。人在群里其实同时在三个维度上动，而且可以
# 一边懒一边开心（躺着刷手机），也可以一边开心一边气（被气笑了）：
#
#   活力：懒散 ←→ 活力   被拉来玩、被夸、话题对味会涨；被催、被冷落会掉
#   情绪：伤心 ←→ 开心   被夸、有好事会涨；坏消息、被无视会掉
#   脾气：生气 ←→ 温柔   被骂、被连续 @ 会掉；被关心、被哄会涨
#
# 每条轴各自取 0~1、各自向基线衰减；被戳到的时候"按住"一段时间不回弹，
# 否则三分钟就淡回原样，等于没变。中性段刻意留宽（基线的上下 0.1~0.2），
# 这样没发生什么的时候提示词里不出现任何状态词——默认状态是"平常"，
# 而不是"懒散"。

# 每段是 (上界, 状态词, 默认提示, 锋利版提示)。锋利版只在 mood.edge >= 0.6 时才拼进去——
# 默认只准"话少"，不准"刻薄"：真实群里有人被冷冰冰的短句整破防过，这个代价不值得。
_EMO_AXES: tuple[dict[str, Any], ...] = (
    {
        "key": "energy",
        "low": "懒散",
        "high": "活力",
        "baseline": 0.42,
        "bands": (
            (0.22, "懒得动", "句子压到最短，能一个字说完就一个字，别主动起话题", ""),
            (0.34, "有点没劲", "短，语气平，别主动往下延伸", ""),
            (0.62, "", "", ""),
            (0.80, "有点来劲", "可以多说三五个字，愿意接对方的话茬", ""),
            (2.00, "来劲了", "可以主动问一句或多带一个细节，但别超过两句", ""),
        ),
    },
    {
        "key": "spirits",
        "low": "伤心",
        "high": "开心",
        "baseline": 0.56,
        "bands": (
            (0.24, "低落", "语气沉一点，别堆语气词；话可以少，但别让人觉得被冷落", ""),
            (0.42, "有点闷", "话少一点，别显得很有兴致", ""),
            (0.66, "", "", ""),
            (0.82, "心情不错", "可以带点语气词，语气松一点", ""),
            (2.00, "挺高兴", "语气轻快，可以跟着对方的情绪走，带点笑意", ""),
        ),
    },
    {
        "key": "temper",
        "low": "生气",
        "high": "温柔",
        "baseline": 0.68,
        "bands": (
            (0.28, "在生气",
             "话少、语气平，不主动往下说；能一句话说完就绝不说两句，但别甩脸色",
             "可以顶一句、可以不好好答，但别骂人"),
            (0.48, "不太耐烦",
             "能少说就少说，但别冷冰冰的",
             "语气不带修饰；对方多问就烦"),
            (0.78, "", "", ""),
            (0.90, "脾气好", "语气软一点，别怼人", ""),
            (2.00, "很温柔", "语气很软，可以顺着对方说、哄一句，但还是一句", ""),
        ),
    },
)

# 什么话会推哪条轴： (关键词, {轴: 增量}, 按住秒数)
# 顺序有意义——先命中的先算。"被说伤人"排最前面：一句道歉或一句"你这话过分了"，
# 要能立刻把脾气拉回来，不能被后面的"有病"抢走。
_EMO_REACTS: tuple[tuple[tuple[str, ...], dict[str, float], float], ...] = (
    (
        ("你这话", "过分了", "伤到", "破防", "委屈", "难受", "我生气了",
         "你怎么这样", "别这样", "对不起", "抱歉", "不好意思", "是我不好"),
        {"temper": +0.32, "spirits": +0.04},
        1800,
    ),
    (
        ("滚", "闭嘴", "傻子", "蠢", "烦死", "讨厌你", "垃圾", "恶心", "你他妈",
         "煞笔", "sb", "SB", "去死", "有病"),
        {"temper": -0.42, "spirits": -0.12},
        1500,
    ),
    (
        ("开黑", "上号", "组队", "打游戏", "来打", "联机", "出来玩", "打球"),
        {"energy": +0.24, "spirits": +0.07},
        900,
    ),
    (
        ("厉害", "可爱", "好看", "喜欢你", "谢谢", "辛苦", "好棒", "牛啊", "好乖"),
        {"spirits": +0.16, "temper": +0.10, "energy": +0.05},
        900,
    ),
    (
        ("累不累", "睡了吗", "怎么了", "没事吧", "抱抱", "摸摸", "别太累",
         "早点睡", "注意身体", "在哭"),
        {"temper": +0.16, "spirits": +0.06},
        1200,
    ),
    (
        ("难过", "想哭", "分手", "失业", "考砸", "生病", "发烧", "住院", "emo", "撑不住"),
        {"spirits": -0.18, "temper": +0.06},
        1500,
    ),
    (
        ("中奖", "发工资", "放假", "赢了", "过了", "成了", "终于"),
        {"spirits": +0.12, "energy": +0.08},
        600,
    ),
    (
        ("催", "怎么还不", "快点", "回我", "说话啊", "人呢"),
        {"temper": -0.14, "energy": +0.06},
        480,
    ),
    (
        ("笑死", "哈哈", "打脸", "你自己说", "不是说不", "翻旧账"),
        {"temper": -0.10, "energy": +0.08},
        360,
    ),
)

_DAY_PARTS = (
    (range(0, 6), "凌晨", "这个点正常人已经困了。回得更短更慢，可以直接说要去睡。"),
    (range(6, 9), "清晨", "刚醒，脑子还没转。回得短一点，可以有点起床气。"),
    (range(9, 12), "上午", "在忙自己的事，回复短、间隔长。"),
    (range(12, 14), "中午", "在吃饭，懒得打字。"),
    (range(14, 18), "下午", "有点困，能少说就少说。"),
    (range(18, 20), "傍晚", "刚要开始自己的时间，不太想被打扰。"),
    (range(20, 23), "晚上", "相对闲一点，可以说两三个字以上。"),
    (range(23, 24), "深夜", "困了，回得又短又慢。"),
)


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


#: 默认的聊天契约模板。配置 prompt_template 可以整段覆盖它。
#: 占位符：{opening} {scope} {fact} {target} {max} {emotion} {time} {recent}
#:         {situation} {facts} {tools} {where}
_CONTRACT_TEMPLATE = """<human_chat_contract>
{opening}
长度：这次只回一句，3~{target} 字，任何情况都不超过 {max} 字。
必须做到：
1. {scope}
2. 先答问题：问去不去就答去/不去，问几点就答几点；答完再决定要不要带态度。
3. 不解释、不列举、不总结、不加「还有什么可以帮你」；不用 Markdown、不用括号写动作或心理、不用引号包整句。
4. 可以敷衍、可以拒绝，但别用空话顶替回答（「知道」「明天说」不算答案）；真不想说自己的私事就把话题岔开。
5. 被问「你是不是机器人/是不是 AI」：别用「你猜」；反问、一句「？」或者岔开，同一个说法不连用两次。
6. 不以「晚安」「先这样」收尾（除非对方先说要睡）；不复述、不总结、不点评对话本身（不说「你问过」「又问一遍」）。
7. 同一立场只说一次；已经翻篇的话题不回头补答；只回最新的一条消息。
8. {fact}
9. 你只说自己真做过的事。别人说「我开车」「我加班」那是别人的情况，不要接成自己也有。
10. 有人起哄你、或者指出你前后不一致时必须有反应：反驳、自嘲、装糊涂都行，不许平静地重复一遍自己的偏好。
11. 状态不好只表现为话少，不表现为刻薄：不阴阳怪气、不嘲讽、不拿对方的糗事或难处说事、不骂人。
12. 回答别太干：哪怕只有两个字，也带个语气词或一句态度，别只甩一个光秃秃的词。
13. 别人只发了图、没写字时，你看不见图里是什么：不许描述画面，回一句「？」「啥图」「看不见」这类反应就行。
{emotion}
{time}
{recent}
{situation}
{facts}
{tools}
</human_chat_contract>"""


class HumanLikePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config: dict = config if isinstance(config, dict) else {}

        self._state_path: Path = (
            StarTools.get_data_dir(PLUGIN_NAME) / "state.json"
        )
        self._state: dict = {"sessions": {}}
        self._last_save: float = 0.0
        self._img_warned: set[str] = set()
        self._save_lock = asyncio.Lock()

    # ───────────────────────── 生命周期 ─────────────────────────

    async def initialize(self) -> None:
        try:
            if self._state_path.exists():
                raw = json.loads(self._state_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("sessions"), dict):
                    self._state = raw
            logger.info(
                f"[humanlike] 已加载 {len(self._state.get('sessions', {}))} 个会话状态"
            )
        except Exception as e:
            logger.warning(f"[humanlike] 状态加载失败，使用空状态: {e}")

        if _WEB_API:
            try:
                self._register_page_api()
                logger.info("[humanlike] 面板 API 已挂上 /api/plug/.../page/*")
            except Exception as e:
                logger.warning(f"[humanlike] 面板 API 注册失败: {e}")

    async def terminate(self) -> None:
        await self._save_state(force=True)

    # ───────────────────────── 配置读取 ─────────────────────────

    def _c(self, path: str, default: Any = None) -> Any:
        """按 'a.b.c' 取配置，任何一层缺失都返回 default。"""
        node: Any = self.config
        for part in path.split("."):
            if not isinstance(node, dict):
                return default
            if part not in node:
                return default
            node = node[part]
        return node

    def _cf(self, path: str, default: float) -> float:
        return _coerce_float(self._c(path, default), default)

    def _ci(self, path: str, default: int) -> int:
        return _coerce_int(self._c(path, default), default)

    def _cb(self, path: str, default: bool) -> bool:
        return _coerce_bool(self._c(path, default), default)

    def _split_ids(self, raw: Any) -> list[str]:
        return [t.strip() for t in re.split(r"[,，\s]+", str(raw or "")) if t.strip()]

    def _targets(self) -> list[str]:
        return self._split_ids(self._c("groups", ""))

    @staticmethod
    def _is_private(event: AstrMessageEvent) -> bool:
        """私聊和群聊的唤醒逻辑完全不同，先认准是哪一种。"""
        umo = str(event.unified_msg_origin or "")
        if "GroupMessage" in umo:
            return False
        if "FriendMessage" in umo:
            return True
        try:
            return not bool(event.get_group_id())
        except Exception:
            return True

    def _enabled_for(self, event: AstrMessageEvent) -> bool:
        if not self._cb("enable", False):
            return False

        # 私聊：对方本来就是冲着你说的，不用等叫，所以不走群号白名单，单独一个开关
        if self._is_private(event):
            if not self._cb("private_enable", False):
                return False
            users = self._split_ids(self._c("private_users", ""))
            if not users:
                return True
            return str(event.get_sender_id() or "") in users

        targets = self._targets()
        if not targets:
            return True
        gid = str(event.get_group_id() or "")
        umo = str(event.unified_msg_origin or "")
        sid = str(event.get_session_id() or "")
        for t in targets:
            if t in (gid, umo, sid) or t in umo:
                return True
        return False

    # ───────────────────────── 会话状态 ─────────────────────────

    def _session(self, umo: str) -> dict:
        sessions = self._state.setdefault("sessions", {})
        sess = sessions.get(umo)
        if not isinstance(sess, dict):
            sess = {}
            sessions[umo] = sess
        sess.setdefault("self_lines", [])
        sess.setdefault("at_times", [])
        sess.setdefault("last_reply_at", 0.0)
        return sess

    # ── 情绪三条轴 ──────────────────────────────────────────

    def _emo_axes(self) -> tuple[dict[str, Any], ...]:
        """把轴定义和配置合起来，让每条轴都能单独改基线。"""
        legacy = self._cf("mood.baseline", 0.56)
        out: list[dict[str, Any]] = []
        for axis in _EMO_AXES:
            spec = dict(axis)
            # 旧配置只有一个 mood.baseline，把它当"情绪"轴的基线接上
            default = legacy if axis["key"] == "spirits" else axis["baseline"]
            spec["baseline"] = self._cf(f"mood.{axis['key']}_baseline", default)
            out.append(spec)
        return tuple(out)

    def _emo_now(self, sess: dict) -> dict[str, float]:
        """读三条轴，各自按经过的时间向基线回归。"""
        axes = self._emo_axes()
        if not self._cb("mood.enable", True):
            return {a["key"]: a["baseline"] for a in axes}

        cur = sess.get("emo")
        if not isinstance(cur, dict):
            # 旧版本只存了一个 mood：把它接到"情绪"轴上，另外两轴回基线
            legacy = sess.pop("mood", None)
            sess.pop("mood_ts", None)
            cur = {
                a["key"]: (
                    _coerce_float(legacy, a["baseline"])
                    if a["key"] == "spirits" and legacy is not None
                    else a["baseline"]
                )
                for a in axes
            }

        last = _coerce_float(sess.get("emo_ts"), time.time())
        hours = max(0.0, (time.time() - last) / 3600.0)
        rate = self._cf("mood.decay_per_hour", 1.0)
        holds = sess.get("emo_hold")
        if not isinstance(holds, dict):
            holds = {}
        now = time.time()

        out: dict[str, float] = {}
        for a in axes:
            key = a["key"]
            value = _coerce_float(cur.get(key), a["baseline"])
            # 刚被戳到的那几分钟几乎不回弹，之后才慢慢淡掉
            held = now < _coerce_float(holds.get(key), 0)
            value += (a["baseline"] - value) * min(1.0, rate * (0.15 if held else 1.0) * hours)
            # 安全阀：脾气再低也不落到这个值以下（关掉"生气"就把它填到 0.45 以上）
            low = self._cf("mood.temper_floor", 0.15) if key == "temper" else 0.02
            out[key] = round(max(low, min(0.98, value)), 4)

        sess["emo"] = out
        sess["emo_ts"] = now
        return out

    def _bump_emo(self, sess: dict, deltas: dict[str, float], hold: float = 0.0) -> dict[str, float]:
        """推一把情绪。hold > 0 表示这段时间内基本不回弹。"""
        if not self._cb("mood.enable", True):
            return self._emo_now(sess)
        cur = self._emo_now(sess)
        out = dict(cur)
        for key, delta in deltas.items():
            if key in out:
                low = self._cf("mood.temper_floor", 0.15) if key == "temper" else 0.02
                out[key] = round(max(low, min(0.98, out[key] + float(delta))), 4)
        sess["emo"] = out
        sess["emo_ts"] = time.time()

        if hold > 0:
            holds = sess.get("emo_hold")
            if not isinstance(holds, dict):
                holds = {}
            for key, delta in deltas.items():
                if key in out and abs(delta) >= 0.04:
                    holds[key] = max(_coerce_float(holds.get(key), 0), time.time() + hold)
            sess["emo_hold"] = holds

        self._schedule_save()
        return out

    def _emo_react(self, sess: dict, text: str, weight: float = 1.0) -> None:
        """按这句话的内容推情绪。只认关键词，不额外调模型。"""
        if not self._cb("mood.enable", True):
            return
        scale = self._cf("mood.react_scale", 1.0) * weight
        if scale <= 0:
            return
        body = text or ""
        for keys, deltas, hold in _EMO_REACTS:
            if not any(k in body for k in keys):
                continue
            self._bump_emo(sess, {k: v * scale for k, v in deltas.items()}, hold)
            logger.debug(f"[humanlike] 情绪变化 {deltas} ← {body[:20]!r}")
            return  # 一句话只按最先命中的那一类算：同时涨同时跌等于没变

    def _remember_self(self, sess: dict, text: str) -> None:
        limit = self._ci("consistency.self_lines", 6)
        if limit <= 0 or not text:
            return
        lines = sess.get("self_lines")
        if not isinstance(lines, list):
            lines = []
        lines.append(text)
        sess["self_lines"] = lines[-limit:]

    def _remember_human(self, sess: dict, name: str, text: str) -> None:
        """记下群里最近几句人话，给反露馅保险当判据。"""
        limit = self._ci("guards.recent_human", 12)
        if limit <= 0 or not text:
            return
        items = sess.get("recent_human")
        if not isinstance(items, list):
            items = []
        items.append({"t": time.time(), "name": name, "text": text[:60]})
        sess["recent_human"] = items[-limit:]

    def _recent_human(self, sess: dict) -> list[dict]:
        items = sess.get("recent_human")
        if not isinstance(items, list):
            return []
        return [i for i in items if isinstance(i, dict)]

    async def _save_state(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_save < 10:
            return
        self._last_save = now
        async with self._save_lock:
            try:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._state_path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(self._state, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                tmp.replace(self._state_path)
            except Exception as e:
                logger.debug(f"[humanlike] 状态保存失败: {e}")

    def _schedule_save(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._save_state())

    # ───────────────────────── 心情与情境 ─────────────────────────

    @staticmethod
    def _emo_band(axis: dict[str, Any], value: float) -> tuple[str, str]:
        """某条轴现在落在哪一段：(状态词, 语气提示)。中性段返回两个空串。"""
        for upper, label, tip, sharp in axis["bands"]:
            if value < upper:
                return label, tip, sharp
        return "", "", ""

    def _emotion_line(
        self,
        sess: dict | None = None,
        *,
        emo: dict | None = None,
        edge: float | None = None,
    ) -> str:
        """把三条轴翻成一句"现在是什么状态"，顺带给出这句该怎么说的约束。

        `emo` / `edge` 传了就按传的算——面板的预览要用它试没保存的基线值。
        """
        if emo is None:
            emo = self._emo_now(sess if isinstance(sess, dict) else {})
        if edge is None:
            edge = self._cf("mood.edge", 0.3)
        labels: list[str] = []
        tips: list[str] = []
        for axis in self._emo_axes():
            label, tip, sharp = self._emo_band(axis, emo.get(axis["key"], axis["baseline"]))
            if not label:
                continue  # 中性段：不写状态，也不给提示
            labels.append(label)
            if tip:
                # 锋利版只在 edge 拉高时才用；默认只准"话少"，不准"刻薄"
                tips.append(f"{tip}；{sharp}" if sharp and edge >= 0.6 else tip)
        if not labels:
            return "现在状态：跟平时一样，不特别高兴也不特别烦。就按平时的语气说。"
        # 正在生气的时候别再叠"开心"那几条：人恼了不会一边恼一边轻快
        if emo.get("temper", 1.0) < 0.48:
            tips = [t for t in tips if "轻快" not in t and "语气松" not in t]
        head = f"现在状态：{'、'.join(labels)}。"
        body = (
            "照这个说，但别把状态本身说出来：" + "；".join(tips) + "。"
            if tips
            else "语气就按这个来。"
        )
        # 情绪低的时候永远跟着这条：可以话少，不能伤人
        low = emo.get("temper", 1.0) < 0.48 or emo.get("spirits", 1.0) < 0.3
        return head + body + ("话少可以，刻薄不行。" if low else "")

    def _target_chars(self, emo: dict) -> int:
        """这一句的目标长度：在 style.target_chars 上按三条轴偏移。"""
        hard = max(4, self._ci("style.max_chars", 20))
        base = {a["key"]: a["baseline"] for a in self._emo_axes()}
        target = self._ci("style.target_chars", 10) + round(
            (emo.get("energy", base["energy"]) - base["energy"]) * 6
            + (emo.get("spirits", base["spirits"]) - base["spirits"]) * 2
            + (emo.get("temper", base["temper"]) - base["temper"]) * 4
        )
        return max(3, min(hard, target))

    def _daypart(self) -> tuple[str, str]:
        hour = datetime.now().hour
        for hours, name, tip in _DAY_PARTS:
            if hour in hours:
                return name, tip
        return "现在", ""

    def _situation_hint(self, text: str) -> str:
        lowered = (text or "").lower()
        for keys, hint in _SITUATIONS:
            for key in keys:
                if key in text or key.lower() in lowered:
                    return hint
        return ""

    def _fact_line(self, sess: dict) -> str:
        """把群里已经说定的事抬进提示词，免得模型当成还没发生。"""
        recent = "，".join(str(i.get("text", "")) for i in self._recent_human(sess)[-8:])
        hits: list[str] = []
        for has_fact, _ in _FACT_CONFLICTS:
            for f in has_fact:
                if f in recent:
                    hits.append(f)
                    break
        if not hits:
            return ""
        return (
            "群里已经说定的事（按已经发生了算，别再提相反的做法）："
            + "、".join(sorted(set(hits)))
            + "。"
        )

    # ───────────────────── 节奏：输出到手之后 ─────────────────────

    def _is_night(self) -> bool:
        now = datetime.now()
        minutes = now.hour * 60 + now.minute
        return minutes >= 23 * 60 + 30 or minutes < 6 * 60

    def _delivery_pause(self, sess: dict) -> float:
        """完整回复已经到手，这一段只在「发出去之前」停一下。

        回不回、要不要接话都归上游管，这里只管一条：这条是秒回，还是隔一会儿再发。
        停在收到完整回复之后，所以既不会拖慢模型，也不会把上游的决策改掉。
        """
        if not self._cb("timing.enable", True):
            return 0.0
        # 正好在看手机：不假装犹豫，直接发
        if random.random() < self._cf("timing.fast_reply_prob", 0.2):
            return 0.0

        lo = max(0.0, self._cf("timing.settle_min", 0.3))
        hi = max(lo, self._cf("timing.settle_max", 1.8))
        pause = random.uniform(lo, hi)

        emo = self._emo_now(sess)
        if emo["energy"] < 0.24 or emo["spirits"] < 0.28:
            pause *= 1.35  # 没劲、情绪低，手也慢
        if emo["temper"] < 0.28:
            pause *= 0.8  # 气头上反而打得又快又短
        if self._is_night() and self._cb("timing.night_slowdown", True):
            pause *= 1.8

        # 重尾：偶尔正好没看手机，停得比平时久，间隔才不规整
        if random.random() < self._cf("timing.long_pause_prob", 0.12):
            tail = random.expovariate(1.0 / 2.5)
            pause += min(tail, max(0.0, self._cf("timing.long_pause_max", 5.0)))

        cap = max(0.0, self._cf("timing.max_delay", 12.0))
        return max(0.0, min(pause, cap))

    def _track_at(self, sess: dict) -> None:
        """记录被 @ 的时间，短时间内连发才掉心情；隔久了自然重置。"""
        window = self._ci("mood.annoy_window", 120)
        now = time.time()
        times = sess.get("at_times")
        if not isinstance(times, list):
            times = []
        times = [t for t in times if now - _coerce_float(t, 0) < window]
        # 上一次呼叫超过 40 秒就算了，说明对方是分几次来找你的，不算刷屏
        if times and now - _coerce_float(times[-1], 0) > 40:
            times = []
        times.append(now)
        sess["at_times"] = times
        if len(times) >= 3:
            step = self._cf("mood.annoy_step", 0.15)
            self._bump_emo(sess, {"temper": -step, "energy": -step * 0.4}, 300)
            sess["at_times"] = [now]

    # ───────────────────── 反露馅硬保险（后处理层） ─────────────────────
    #
    # 提示词只能"劝"，这层是"拦"：模型真的写了会露馅的句子时，按结构把它切掉。
    # 全部是可单独关掉的开关，见配置 guards.*。

    @staticmethod
    def _clause_drop(text: str, bad) -> tuple[str, int]:
        """按逗号切小句，丢掉命中 bad 的小句，返回 (剩余文本, 丢掉几句)。"""
        clauses = [c for c in re.split(r"(?<=[，,、])", text or "") if c]
        kept: list[str] = []
        dropped = 0
        for c in clauses:
            if bad(c):
                dropped += 1
                continue
            kept.append(c)
        out = "".join(kept)
        out = re.sub(r"[，,、]{2,}", "，", out).strip().strip("，,、").strip()
        return out, dropped

    def _guard_attribute_transfer(self, text: str, sess: dict) -> str:
        """别人说的「我开车」不是你的经历，别接成自己也有。"""
        if not self._cb("guards.attribute_transfer", True):
            return text
        others: set[str] = set()
        for item in self._recent_human(sess):
            others |= _self_claims(str(item.get("text", "")))
        if not others:
            return text
        own: set[str] = set()
        for line in sess.get("self_lines") or []:
            own |= _self_claims(str(line))
        transferred = others - own
        if not transferred:
            return text

        out, dropped = self._clause_drop(text, lambda c: bool(_self_claims(c) & transferred))
        if dropped:
            logger.info(f"[humanlike] 拦下属性错配 {sorted(transferred)}：{text!r} → {out!r}")
        return out

    def _guard_fact_conflict(self, text: str, sess: dict) -> str:
        """群里已经订好位了，就别再说"先占个位"。"""
        if not self._cb("guards.fact_conflict", True):
            return text
        recent = "，".join(str(i.get("text", "")) for i in self._recent_human(sess)[-8:])
        for has_fact, bad_acts in _FACT_CONFLICTS:
            if not any(f in recent for f in has_fact):
                continue
            if not any(a in text for a in bad_acts):
                continue
            out, dropped = self._clause_drop(text, lambda c, b=bad_acts: any(a in c for a in b))
            if dropped:
                logger.info(f"[humanlike] 拦下事实冲突（群里已说定）：{text!r} → {out!r}")
            return out
        return text

    def _guard_stance_repeat(self, text: str, sess: dict) -> tuple[str, bool]:
        """「别太辣」说过了就不再说第二遍；返回 (剩余文本, 是否命中复读)。"""
        if not self._cb("guards.dedupe_stance", True):
            return text, False
        prior: set[str] = set()
        for line in (sess.get("self_lines") or [])[-4:]:
            prior |= _stance_keys(str(line))
        if not prior:
            return text, False
        out, dropped = self._clause_drop(text, lambda c: bool(_stance_keys(c) & prior))
        if dropped:
            logger.info(f"[humanlike] 拦下立场复读 {sorted(prior)}：{text!r} → {out!r}")
        return out, dropped > 0

    def _guard_stale(self, text: str, sess: dict) -> bool:
        """话题已经翻篇了还回头补答：这句在答旧问题，而不是最新那句。

        ⚠️ 默认关闭。它靠"话题字重合"判断，而正常的回答也常常要引用上文
        （「反正有开车的，我拎两瓶吧」就是在接上一句说谁开车），会误杀。
        真正那批"回头补答"其实都是同一句约束复述，已经被 _guard_stance_repeat 拦掉了。
        """
        if not self._cb("guards.drop_stale", False):
            return False
        items = self._recent_human(sess)
        if len(items) < 2:
            return False
        newest_t = _coerce_float(items[-1].get("t"), 0)
        reply_topic = _topic_set(text)
        if len(reply_topic) < 2:
            return False
        overlap_new = len(reply_topic & _topic_set(str(items[-1].get("text", ""))))
        if overlap_new >= 2:
            return False
        for item in items[:-1]:
            if _coerce_float(item.get("t"), 0) >= newest_t:
                continue
            if len(reply_topic & _topic_set(str(item.get("text", "")))) >= 2:
                logger.info(
                    f"[humanlike] 拦下滞后补答（已翻篇）：{text!r} ← {item.get('text')!r}"
                )
                return True
        return False

    def _tease_kind(self, sess: dict) -> str:
        """这一轮是不是在被人起哄 / 被指出前后不一致；返回 ''/deny/laugh。"""
        since = _coerce_float(sess.get("last_reply_at"), 0)
        texts = [
            str(i.get("text", ""))
            for i in self._recent_human(sess)[-3:]
            if _coerce_float(i.get("t"), 0) >= since
        ]
        for t in texts:
            if any(m in t for m in _TEASE_DENY):
                return "deny"
        for t in texts:
            if any(m in t for m in _TEASE_STRONG):
                return "laugh"
            if any(m in t for m in _TEASE_WEAK) and ("你" in t or "他" in t):
                return "laugh"
        return ""

    def _is_teased(self, sess: dict) -> bool:
        return bool(self._tease_kind(sess))

    def _reaction(self, sess: dict, kind: str = "laugh") -> str:
        """取一句还没怎么用过的反应，避免连着复读同一句。"""
        pool = _REACT_DENY if kind == "deny" else _REACT_LAUGH
        used = sess.get("react_used")
        if not isinstance(used, list):
            used = []
        counts = {w: used.count(w) for w in pool}
        low = min(counts.values())
        cand = [w for w in pool if counts[w] == low]
        if len(cand) > 1 and used and used[-1] in cand:
            cand = [w for w in cand if w != used[-1]]
        pick = random.choice(cand)
        sess["react_used"] = (used + [pick])[-12:]
        return pick

    # ───────────────────────── 钩子：只记事，不裁决 ─────────────────────────

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE
    )
    async def note_incoming(self, event: AstrMessageEvent) -> None:
        """把这句话记下来，并按它推一下情绪。

        「这条要不要回」「要不要主动接一句」都不归这里管——那是上游（AstrBot 自己、
        Proactive Chat、angel_heart）的活。插件要是再判一次，两边就会打架：
        一边想回、一边按概率掐掉，最后表现成"有时候莫名其妙不理人"。
        这里只做两件上游做不到的事：记住人话（反露馅保险要拿它当判据），
        以及让被夸被骂真的会改变它的语气。
        """
        try:
            if not self._enabled_for(event):
                return

            text = (event.get_message_str() or "").strip()
            if not text or text.startswith("/"):
                return

            umo = str(event.unified_msg_origin or "")
            sess = self._session(umo)

            try:
                name = str(event.get_sender_name() or "").strip()
            except Exception:
                name = ""
            self._remember_human(sess, name, text)

            # 被夸、被骂、被催、被拉去玩都会推情绪。没叫到自己时只算旁观，
            # 只推四成——群里别人的话会影响心情，但不该跟直接冲你说的一样重。
            private = self._is_private(event)
            addressed = private or bool(getattr(event, "is_at_or_wake_command", False))
            self._emo_react(sess, text, 1.0 if addressed else 0.4)

            # 短时间被连着 @ 才会掉脾气；私聊里没有"@到自己"这回事
            if not private and getattr(event, "is_at_or_wake_command", False):
                self._track_at(sess)

            self._schedule_save()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[humanlike] note_incoming 出错: {e}", exc_info=True)

    # ───────────────────────── 钩子：提示词契约 ─────────────────────────

    @filter.on_llm_request()
    async def shape_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        try:
            if not self._enabled_for(event):
                return
            umo = str(event.unified_msg_origin or "")
            sess = self._session(umo)

            # 历史里格式不合法的图片块会让 text-only 的模型整轮 400，
            # 换成 [图片] 至少这一句还能正常说
            fixed = self._scrub_history_images(req)
            if fixed and umo not in self._img_warned:
                # 历史不会被改写，所以每个会话只提醒一次，不然每条消息都要刷一行
                self._img_warned.add(umo)
                logger.info(
                    f"[humanlike] 历史里有 {fixed} 处图片块格式不被模型接受，"
                    f"已换成 [图片]：{umo}"
                )

            # 只发「正在输入」，不加延迟——节奏放在输出到手之后（_delivery_pause）
            if self._cb("timing.typing_indicator", True):
                try:
                    await event.send_typing()
                except Exception:
                    pass

            contract = self._build_contract(event, sess, req)
            req.system_prompt = (req.system_prompt or "") + "\n\n" + contract

            # 只在重载后的第一条上打一次：确认 reply_to 这类工具真的挂上了，
            # 不然提示词里那句「可以引用」就是空话
            if not getattr(self, "_tools_logged", False):
                self._tools_logged = True
                try:
                    names = list(req.func_tool.names()) if getattr(req, "func_tool", None) else []
                except Exception:
                    names = []
                logger.info(
                    f"[humanlike] 本轮可用工具 {len(names)} 个，"
                    f"含 reply_to={'reply_to' in names}：{names[:10]}"
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[humanlike] shape_request 出错: {e}", exc_info=True)

    def _scrub_history_images(self, req: Any) -> int:
        """把历史里格式不合法的图片块换成文字占位，返回改了几处。

        AstrBot 只会处理「能解析」的 image_url；url 缺失、或者不是 data:/http
        的那一块会被原样发给模型，text-only 的模型直接返回
        `Unsupported image_url format`，整轮回复就没了，而且报错会被当成回复发出去。
        """
        if not self._cb("guards.scrub_images", True):
            return 0
        fixed = 0
        for msg in getattr(req, "contexts", None) or []:
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for idx, part in enumerate(content):
                if not isinstance(part, dict) or part.get("type") != "image_url":
                    continue
                box = part.get("image_url")
                url = box.get("url") if isinstance(box, dict) else None
                if isinstance(url, str) and url.startswith(_OK_IMAGE_PREFIX):
                    continue
                content[idx] = {"type": "text", "text": "[图片（看不到内容）]"}
                fixed += 1
        return fixed

    def _build_contract(
        self, event: AstrMessageEvent, sess: dict, req: Any = None
    ) -> str:
        hard_max = max(4, self._ci("style.max_chars", 20))  # 配置写坏了也不至于把回复剪成两个字
        # 回多长也跟着状态走：有劲的时候多说两个词，没劲、生气就压短
        emo = self._emo_now(sess)
        target = max(3, min(hard_max, self._target_chars(emo)))
        daypart, time_tip = self._daypart()

        private = self._is_private(event)
        where = "这段私聊" if private else "这个群"
        user_text = (event.get_message_str() or "").strip()

        remembered = sess.get("self_lines") or []
        if remembered:
            recent = "；".join(f"「{line}」" for line in remembered[-4:])
            recent_text = f"你最近在{where}里说过：{recent}。保持同样的立场和语气，但不要复读原句。"
        else:
            recent_text = f"你在{where}里还没怎么说过话。"

        situation = self._situation_hint(user_text)
        if self._is_teased(sess):
            tease = (
                "刚才有人在起哄你、或者指出你前后不一致。"
                "这一句必须有反应（反驳/自嘲/装糊涂都行），不要平静地复述自己的偏好。"
            )
            situation = f"{situation}\n{tease}" if situation else tease

        values = {
            "where": where,
            "target": target,
            "max": hard_max,
            "opening": (
                "你是在 QQ 上跟人一对一聊天的真人。不是助手，不是客服，也不是在表演角色扮演。"
                if private
                else "你是在 QQ 群里聊天的真人。不是助手，不是客服，也不是在表演角色扮演。"
            ),
            "scope": (
                "这是私聊，对方就是在跟你说话，不用等叫，也别问「你找我干嘛」。"
                if private
                else "群聊里别人互相说话不用你插嘴，你只回叫到你的那一句。"
            ),
            "fact": (
                "你们已经说定的事（几点、去哪、谁先到）按它已经发生来算，不要再提相反的做法。"
                if private
                else "群里已经说定的事（谁订了位、谁带了什么）按它已经发生来算，不要再提相反的做法。"
            ),
            "emotion": self._emotion_line(sess),
            "time": f"现在时间：{datetime.now():%H:%M}（{daypart}）。{time_tip}",
            "recent": recent_text,
            "situation": f"当前情境：{situation}" if situation else "",
            "facts": self._fact_line(sess),
            "tools": "" if private else self._tools_line(req),
        }

        # prompt_template 非空就整段用它；留空用内置默认
        template = str(self._c("prompt_template", "") or "").strip() or _CONTRACT_TEMPLATE
        body = template
        for key, value in values.items():
            body = body.replace("{" + key + "}", str(value))
        body = re.sub(r"\n{3,}", "\n\n", body).strip()

        # 手动补的提示词：永远接在最后，优先级最高（模板里写不写 {extra} 都不影响）
        extra = str(self._c("prompt_extra", "") or "").strip()
        if extra:
            body = body.rstrip() + f"\n额外要求（优先于上面的所有条款）：{extra}"
        return body

    # ───────────────────── 引用 / 艾特：把话对准人 ─────────────────────

    @staticmethod
    def _has_tool(req: Any, name: str) -> bool:
        """看一眼这次的请求里到底有没有这个工具，没有就别在提示词里画饼。"""
        toolset = getattr(req, "func_tool", None)
        if toolset is None:
            return False
        try:
            names = getattr(toolset, "names", None)
            if callable(names):
                names = names()
            if isinstance(names, (list, tuple, set)):
                return name in names
        except Exception:
            pass
        try:
            return any(
                getattr(t, "name", "") == name for t in getattr(toolset, "tools", [])
            )
        except Exception:
            return False

    def _tools_line(self, req: Any) -> str:
        """引导模型在合适的时候用引用 / 艾特。

        QQ 工具箱把这两件事做成了「模型主动用才有」的能力：工具列表里有 reply_to，
        正文里写 [At:QQ号] 会被转成真实艾特。但模型默认几乎不会主动用——除非提示词
        明确写了「什么时候该用」。这里补的就是这一句：让对话有指向性，
        又别变成每条消息都挂一个 @。
        """
        if not self._cb("tools.guide", True):
            return ""
        want_quote = self._cb("tools.reply_quote", True) and self._has_tool(req, "reply_to")
        want_at = self._cb("tools.at_user", True)
        if not (want_quote or want_at):
            return ""

        lines = ["想让这句话落到具体某个人头上，可以用 QQ 的引用 / 艾特——只在真需要的时候用："]
        if want_quote:
            lines.append(
                "· 引用：调用 reply_to 工具（不传参数就是引用你正在回的这一条）。"
                "这条要回的不是最新那条、或者群里同时有两三个话题在跑时，默认引用那一条，"
                "别只发一句裸的——让人一眼看出你在回谁。"
            )
        if want_at:
            lines.append(
                "· 艾特：在正文开头写 [At:QQ号]，QQ 号就是上下文里的「ID: xxxx」「User ID: xxxx」。"
                "用在你要把话递给**另一个人**的时候：点名让他接话、问他问题、把话题丢给他。"
                "如果对方刚 @ 过你、或者你就是在回他刚发的那条，不要 @ 回去，那样像复读机。"
            )
        lines.append(
            "一条回复最多加一个，别每条都加，也别为了显得礼貌而加；"
            "本来就在回他刚发的那条、或者一对一说话时，不需要。"
        )
        return "\n".join(lines)

    # ───────────────────────── 钩子：形态清洗 ─────────────────────────

    @filter.on_decorating_result()
    async def humanize_result(self, event: AstrMessageEvent) -> None:
        try:
            if not self._cb("enable", False):
                return
            result = event.get_result()
            if result is None or not result.chain:
                return

            # ① 最先做：这条是不是上游报错？是就只写日志，绝不发出去。
            #    报错正文里带着 provider 返回的 JSON，发进群里比不说话糟得多。
            if self._cb("guards.hide_errors", True) and self._is_error_result(
                event, result
            ):
                logger.error(
                    "[humanlike] 拦下一条要发到群里的报错（只记录，不发送）："
                    f"{result.get_plain_text()[:400]!r}"
                )
                event.clear_result()
                return

            if not self._enabled_for(event):
                return
            if not result.is_model_result():
                return

            original = result.get_plain_text()
            if not (original or "").strip():
                return

            umo = str(event.unified_msg_origin or "")
            sess = self._session(umo)

            cleaned = self._sanitize(
                original,
                await self._self_names(event) if self._cb("style.plain", False) else (),
            )
            tease = self._tease_kind(sess)

            # 四道硬保险：改不了"生成"，但可以拦下"不该发出去的那句"
            cleaned = self._guard_attribute_transfer(cleaned, sess)
            cleaned = self._guard_fact_conflict(cleaned, sess)
            cleaned, repeated = self._guard_stance_repeat(cleaned, sess)
            if cleaned and self._guard_stale(cleaned, sess):
                cleaned = ""

            followups: list[str] = []
            if not cleaned:
                # 整句都是会被抓的复读：如果是被起哄，就干脆改成给一句反应
                if tease and self._cb("guards.tease_reaction", True):
                    cleaned = self._reaction(sess, tease)
                    logger.info(f"[humanlike] 复读被拦，改为给反应：{cleaned!r}")
                else:
                    logger.info(f"[humanlike] 这条会露馅，选择不发：{original!r}")
                    event.clear_result()
                    return
            elif tease and self._cb("guards.tease_reaction", True):
                if not any(m in cleaned for m in _REACTION_MARKS):
                    # 答完之后再补一条反应，而不是硬塞进同一句——真人就是这么连发的
                    followups.append(self._reaction(sess, tease))
                    logger.info(f"[humanlike] 被起哄，追加一条反应：{followups[-1]!r}")

            self._remember_self(sess, cleaned)
            self._schedule_save()

            # 保留引用 / @ 等非文本组件，只重建文本部分
            keep: list[Any] = [
                comp for comp in result.chain if not isinstance(comp, Comp.Plain)
            ]
            bubbles = self._split_bubbles(cleaned, sess)
            if followups and len(bubbles) > 1:
                followups = []  # 已经连发两条了，别再补第三条
            bubbles.extend(followups)

            new_result = MessageEventResult()
            try:
                new_result.result_content_type = result.result_content_type
            except Exception:
                pass
            for comp in keep:
                new_result.chain.append(comp)
            sent = 0
            for bubble in bubbles:
                # 空气泡会在群里渲染成一条空白消息——比说错话更像机器，直接丢
                if bubble and bubble.strip():
                    new_result.message(bubble)
                    sent += 1
            if not sent:
                event.clear_result()
                return

            # 完整回复已经到手，这里才决定"这条隔多久发出去"——秒回，还是停一下。
            pause = self._delivery_pause(sess)
            if pause > 0:
                await asyncio.sleep(pause)
            sess["last_reply_at"] = time.time()
            self._schedule_save()

            event.set_result(new_result)
            logger.debug(f"[humanlike] {original!r} → {bubbles!r}（停顿 {pause:.1f}s）")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[humanlike] humanize_result 出错: {e}", exc_info=True)

    @staticmethod
    def _is_error_result(event: AstrMessageEvent, result: Any) -> bool:
        """这条结果是不是上游报错。

        三种判据，任意命中就算：结果类型是 runner error、事件上挂了第三方 runner 的
        错误标记、或者正文就是以 AstrBot 的报错模板开头的。
        """
        ctype = getattr(result, "result_content_type", None)
        if "ERROR" in str(getattr(ctype, "name", "")).upper():
            return True
        try:
            if event.get_extra("_third_party_runner_error"):
                return True
        except Exception:
            pass
        text = (result.get_plain_text() or "")[:600]
        head = text.lstrip()
        if any(head.startswith(p) for p in _ERROR_HEADS):
            return True
        # `LLM 响应错误: ... Error code: 400 - {'error': {...}}` 这类模板
        return all(p in head for p in _ERROR_BODY)

    # ───────────────────────── 文本处理 ─────────────────────────

    def _drop_ai_sentences(self, text: str) -> str:
        t = _RE_AI_LEAD.sub("", (text or "").strip())
        sentences = [s for s in _RE_SENT_SPLIT.split(t) if s and s.strip()]
        if not sentences:
            return t
        kept = [
            s
            for s in sentences
            if not any(p in s.lower() for p in _AI_PHRASES)
        ]
        out = "".join(kept) if kept else sentences[0]
        return _RE_AI_LEAD.sub("", out.strip())

    async def _self_names(self, event: AstrMessageEvent) -> tuple[str, ...]:
        """这个会话里它该被怎么称呼：配置优先，否则读当前会话的人设名。

        只在「隐形模式」把自称换成「我」时用得到，所以取不到也不算错——
        插件其余部分完全不依赖人格叫什么。
        """
        raw = str(self._c("persona_name", "") or "")
        names = [n.strip() for n in re.split(r"[,，、\s]+", raw) if n.strip()]

        if not names:
            try:
                pm = getattr(self.context, "persona_manager", None)
                if pm is not None:
                    persona = await pm.get_default_persona_v3(
                        getattr(event, "unified_msg_origin", None)
                    )
                    name = str((persona or {}).get("name") or "").strip()
                    if name:
                        names.append(name)
                        # 「阿岚（认主）」这种带后缀的，本体也一起换
                        head = re.split(r"[（(]", name)[0].strip()
                        if head and head != name:
                            names.append(head)
            except Exception as e:
                logger.debug(f"[humanlike] 读人设名失败（不影响其他层）: {e}")

        # 长的先换，免得短名先吃掉长名的一部分
        return tuple(sorted(set(names), key=len, reverse=True))

    def _apply_tics(self, text: str) -> str:
        """把口癖（默认「喵」）按概率换成随机语气词。

        只在两种位置换，别的一律不动：
        - **句末/分句末**（后面是标点或没有了）→ 一般语气词（啊/吧/呢…）；
        - **句首**（在开头或上一个标点之后，紧跟「，。！？…」）→ 感叹词（唔/哦/诶…）。
        句中的不换：实测盲替换会把「随便喵一句」改成「随便呀一句」，病句。
        """
        if not text or self._cb("style.plain", False):
            return text
        tic = str(self._c("style.tic_from", "") or "").strip()
        if not tic or tic not in text:
            return text
        prob = self._cf("style.tic_prob", 0.0)
        if prob <= 0:
            return text
        pool = self._split_ids(self._c("style.tic_to", ""))
        if not pool:
            return text
        lead_pool = list(_TIC_LEAD)

        last = {"pick": ""}

        def _pick(options: list[str]) -> str:
            cand = [o for o in options if o != last["pick"]] or options
            chosen = random.choice(cand)
            last["pick"] = chosen
            return chosen

        out: list[str] = []
        i = 0
        while True:
            j = text.find(tic, i)
            if j < 0:
                out.append(text[i:])
                break
            out.append(text[i:j])
            prev = text[j - 1] if j > 0 else ""
            nxt = text[j + len(tic)] if j + len(tic) < len(text) else ""
            at_clause_start = (not prev) or prev in _CLAUSE_BREAKS
            at_clause_end = (not nxt) or nxt in _CLAUSE_BREAKS
            if at_clause_start and nxt and nxt in _CLAUSE_BREAKS and random.random() < prob:
                out.append(_pick(lead_pool))
            elif at_clause_end and random.random() < prob:
                out.append(_pick(pool))
            else:
                out.append(tic)
            i = j + len(tic)
        return "".join(out)

    def _sanitize(self, text: str, self_names: tuple[str, ...] = ()) -> str:
        t = (text or "").strip()
        if not t:
            return ""

        # [At:123456] 是留给 QQ 工具箱转真艾特的标记，先藏成占位符再清洗，
        # 免得被 Markdown 清洗或者按字截断切开
        stash: list[str] = []

        def _stash_at(m: re.Match) -> str:
            stash.append(m.group(0))
            return f"@@AT{len(stash) - 1}@@"

        t = _RE_AT_MARK.sub(_stash_at, t)

        if self._cb("style.strip_markdown", True):
            t = _RE_CODE.sub(r"\1", t)
            t = _RE_LINK.sub(r"\1", t)
            t = _RE_BOLD.sub(r"\1", t)
            t = _RE_UNDER.sub(r"\1", t)
            t = _RE_ITALIC.sub(r"\1", t)
            t = _RE_HEAD.sub("", t)
            t = _RE_LIST.sub("", t)
            t = _RE_QUOTE.sub("", t)

        if self._cb("style.strip_action", True):
            t = _RE_ACTION.sub("", t)

        if self._cb("style.plain", False):
            # 「隐形模式」：去掉口癖，改成普通人类打字
            for name in self_names:
                t = t.replace(name, "我")
            t = re.sub(r"[～~]+", "", t)
            t = re.sub(r"…+", "", t)
            t = re.sub(r"\.{3,}", "", t)
            t = t.replace("喵", "")
            t = _RE_INLINE_SPACE.sub(" ", t).strip()

        t = _RE_EMOJI.sub("", t)
        t = self._drop_ai_sentences(t)
        t = self._apply_tics(t)
        t = _RE_INLINE_SPACE.sub(" ", t)
        t = _RE_BLANK.sub(" ", t).strip()
        t = t.strip("`*_#>-· ")

        if self._cb("style.drop_trailing_period", True):
            t = _RE_TRAILING_DOT.sub("", t)

        if not self._cb("style.keep_ellipsis", True):
            t = t.replace("…", "").replace("...", "")

        # 把藏起来的 [At:123456] 放回去。截断是按字算的，标记要是被切成半截，
        # 发出去就是一条带方括号的怪话——所以先还原，再截断，最后修掉残口。
        if stash:
            t = _RE_AT_TOKEN.sub(
                lambda m: stash[int(m.group(1))]
                if int(m.group(1)) < len(stash)
                else "",
                t,
            )

        hard_max = max(4, self._ci("style.max_chars", 20))
        if len(t) > hard_max:
            t = self._clip(t, hard_max)
        if "[At:" in t:
            t = _RE_AT_TRUNC.sub("", t).rstrip("…")
        return t.strip()

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        """截断到 limit 字，尽量停在标点处，并补一个省略号。"""
        limit = max(2, limit)
        # 预留一个省略号的位置，保证最终长度不超过 limit
        head = text[: limit - 1]
        breaks = ("，", "、", " ", "~", "～", "…")
        cut_at = max((head.rfind(p) for p in breaks), default=-1)
        if cut_at >= max(2, int(limit * 0.5)):
            head = head[:cut_at]
        head = head.rstrip("，,、 ")
        if head and head[-1] not in "…！？!?~～":
            head += "…"
        return head

    def _split_bubbles(self, text: str, sess: dict | None = None) -> list[str]:
        """偶尔把一句话拆成两条，模拟真人的连发。"""
        # 开头的艾特标记不算正文：它必须和第一句话待在同一条消息里，
        # 单独发出去就是一条「@某人」的空白消息
        lead = ""
        m = _RE_AT_LEAD.match(text)
        if m:
            lead, text = m.group(1), text[m.end() :]
        if len(text) < 7:
            return [lead + text]

        prob = self._cf("style.split_prob", 0.35)
        if sess is not None:
            prob *= 0.7 + 0.8 * self._emo_now(sess)["energy"]  # 有劲的时候更爱连发
        if prob <= 0 or random.random() >= prob:
            return [lead + text]
        cut = -1
        for mark in ("，", ",", "、", " "):
            idx = text.find(mark)
            if idx >= 3:
                cut = idx
                break
        if cut < 0:
            return [lead + text]
        # 拆分点上的标点要留在前半句：以前是 head = text[:cut]，等于每拆一次就吃掉一个逗号，
        # 长句子一多就会发现「怎么全是没标点的短句」。真人连发时也是「今天天气不错，」「我们去吃饭吧。」
        head = text[: cut + 1].strip()
        tail = text[cut + 1 :].strip()
        if len(head) < 2 or len(tail) < 2:
            return [lead + text]
        return [lead + head, tail]

    # ─────────────────── 面板页面 API（pages/dashboard） ───────────────────
    #
    # 页面本身是静态的 pages/dashboard/index.html，数据走这里：
    #   GET  /api/plug/astrbot_plugin_humanlike/page/config   读配置 + 轴定义 + 会话
    #   POST /api/plug/astrbot_plugin_humanlike/page/config   存配置（写文件并就地生效）
    #   GET  /api/plug/astrbot_plugin_humanlike/page/preview  试一组轴值的文案
    #   GET  /api/plug/astrbot_plugin_humanlike/page/state    各会话当前情绪
    #   POST /api/plug/astrbot_plugin_humanlike/page/state    哄一下 / 回到基线
    #
    # 存配置不重启：写盘之后把 self.config 就地换掉，所有 _cf/_cb 读的就是新值。

    def _register_page_api(self) -> None:
        reg = self.context.register_web_api
        p = PAGE_API_PREFIX
        reg(f"{p}/config", self.api_get_config, ["GET"], "人味层：读取配置与会话")
        reg(f"{p}/config", self.api_save_config, ["POST"], "人味层：保存配置")
        reg(f"{p}/preview", self.api_preview, ["GET"], "人味层：预览情绪文案")
        reg(f"{p}/state", self.api_get_state, ["GET"], "人味层：会话情绪状态")
        reg(f"{p}/state", self.api_write_state, ["POST"], "人味层：哄一下 / 回到基线")

    def _schema(self) -> dict:
        try:
            raw = (Path(__file__).parent / "_conf_schema.json").read_text(encoding="utf-8-sig")
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _config_path(self) -> Path:
        """<data>/plugin_data/<plugin>/state.json → <data>/config/<plugin>_config.json"""
        return self._state_path.parents[1] / "config" / f"{PLUGIN_NAME}_config.json"

    @staticmethod
    def _clamp_known(path: str, value: float) -> float:
        key = path.rsplit(".", 1)[-1]
        if key in ("max_chars", "target_chars", "decay_per_hour"):
            floor = {"max_chars": 8.0, "target_chars": 3.0}.get(key, 0.0)
            return max(floor, min(86400.0, value))
        if key in ("settle_min", "settle_max", "long_pause_max", "max_delay"):
            return max(0.0, min(120.0, value))
        if key == "annoy_window":
            return max(5.0, min(86400.0, value))
        if key == "react_scale":
            return max(0.0, min(3.0, value))
        if key.endswith(("_baseline", "_prob", "_step")) or key in (
            "edge",
            "temper_floor",
            "prob",
            "split_prob",
        ):
            return max(0.0, min(1.0, value))
        return value

    def _coerce_node(self, value: Any, spec: dict, path: str, problems: list[str]) -> Any:
        kind = str(spec.get("type") or "").lower()
        if kind == "object":
            items = spec.get("items") or {}
            if not isinstance(value, dict):
                problems.append(f"{path} 应该是对象，已忽略")
                return {}
            out: dict[str, Any] = {}
            for k, v in value.items():
                sub = items.get(k)
                if not isinstance(sub, dict):
                    problems.append(f"忽略未知项 {path}.{k}")
                    continue
                out[k] = self._coerce_node(v, sub, f"{path}.{k}", problems)
            return out
        if kind == "bool":
            return _coerce_bool(value, False)
        if kind == "int":
            return int(self._clamp_known(path, _coerce_int(value, 0)))
        if kind == "float":
            return round(self._clamp_known(path, _coerce_float(value, 0.0)), 4)
        return "" if value is None else str(value)

    def _merge_config(self, incoming: dict) -> tuple[dict, list[str]]:
        """按 _conf_schema.json 校验一遍：未知键丢掉，类型不对的强转，已知范围夹紧。"""
        schema = self._schema()
        # self.config 是 AstrBotConfig（dict 子类，内部带锁），不能 deepcopy，
        # 走一次 JSON 往返拿干净的纯 dict。
        merged: dict = json.loads(json.dumps(self.config, ensure_ascii=False))
        problems: list[str] = []
        for key, value in incoming.items():
            spec = schema.get(key)
            if not isinstance(spec, dict):
                problems.append(f"忽略未知项 {key}")
                continue
            merged[key] = self._coerce_node(value, spec, key, problems)
        return merged, problems

    def _write_config_file(self, cfg: dict) -> None:
        path = self._config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "\ufeff" + json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def _session_brief(self, limit: int = 40) -> list[dict]:
        sessions = self._state.get("sessions") or {}
        out: list[dict] = []
        for umo, sess in sessions.items():
            if not isinstance(sess, dict):
                continue
            try:
                emo = self._emo_now(sess)
            except Exception:
                continue
            labels: list[str] = []
            for axis in self._emo_axes():
                label, _, _ = self._emo_band(axis, emo.get(axis["key"], axis["baseline"]))
                if label:
                    labels.append(label)
            kind = "private" if "FriendMessage" in str(umo) else "group"
            out.append(
                {
                    "umo": umo,
                    "kind": kind,
                    "name": str(umo).rsplit(":", 1)[-1],
                    "emo": {k: round(float(v), 3) for k, v in emo.items()},
                    "labels": labels,
                    "line": self._emotion_line(emo=emo),
                    "last_reply_at": _coerce_float(sess.get("last_reply_at"), 0),
                }
            )
        out.sort(key=lambda item: -(item.get("last_reply_at") or 0))
        return out[:limit]

    def _meta(self) -> dict:
        baselines = {a["key"]: a["baseline"] for a in self._emo_axes()}
        axes = []
        for axis in _EMO_AXES:
            axes.append(
                {
                    "key": axis["key"],
                    "low": axis["low"],
                    "high": axis["high"],
                    "baseline": round(baselines.get(axis["key"], axis["baseline"]), 4),
                    "bands": [
                        [upper, label, tip, sharp] for upper, label, tip, sharp in axis["bands"]
                    ],
                }
            )
        return {
            "version": _PLUGIN_VERSION,
            "axes": axes,
            "sharp_edge": 0.6,
            "template": _CONTRACT_TEMPLATE,
            "config_path": str(self._config_path()),
        }

    async def api_get_config(self):
        try:
            return json_response(
                {
                    "status": "ok",
                    "data": {
                        "config": self.config,
                        "meta": self._meta(),
                        "sessions": self._session_brief(),
                    },
                }
            )
        except Exception as e:
            logger.error(f"[humanlike] 读取配置失败: {e}", exc_info=True)
            return error_response(f"读取配置失败：{e}", status_code=500)

    async def api_save_config(self):
        try:
            return await self._do_save_config()
        except Exception as e:  # 兜底：别让面板只看到一句 Internal server error
            logger.exception("[humanlike] 保存配置失败")
            return error_response(f"保存失败：{e}", status_code=500)

    async def _do_save_config(self):
        try:
            payload = await web_request.json(default={}) or {}
        except Exception as e:
            return error_response(f"请求体解析失败：{e}")
        incoming = payload.get("config") if isinstance(payload, dict) else None
        if not isinstance(incoming, dict):
            return error_response("缺少 config")

        merged, problems = self._merge_config(incoming)
        self.config.clear()
        self.config.update(merged)
        try:
            # AstrBotConfig 自己知道配置该写去哪，优先让它写（原子替换）
            saver = getattr(self.config, "save_config", None)
            if callable(saver):
                saver()
            else:
                self._write_config_file(merged)
        except Exception as e:
            logger.error(f"[humanlike] 写配置失败: {e}", exc_info=True)
            return error_response(f"写配置失败：{e}", status_code=500)

        logger.info(f"[humanlike] 面板改了配置：{list(incoming.keys())}")
        return json_response(
            {"status": "ok", "data": {"config": merged, "warnings": problems}}
        )

    async def api_preview(self):
        try:
            q = web_request.query

            def num(name: str, default: float) -> float:
                try:
                    return max(0.0, min(1.0, float(q.get(name, default))))
                except (TypeError, ValueError):
                    return default

            emo = {
                "energy": num("energy", self._cf("mood.energy_baseline", 0.42)),
                "spirits": num("spirits", self._cf("mood.spirits_baseline", 0.56)),
                "temper": num("temper", self._cf("mood.temper_baseline", 0.68)),
            }
            edge = num("edge", self._cf("mood.edge", 0.3))
            labels = []
            for axis in self._emo_axes():
                label, _, _ = self._emo_band(axis, emo[axis["key"]])
                if label:
                    labels.append(label)
            return json_response(
                {
                    "status": "ok",
                    "data": {
                        "line": self._emotion_line(emo=emo, edge=edge),
                        "labels": labels,
                        "target": self._target_chars(emo),
                    },
                }
            )
        except Exception as e:
            logger.error(f"[humanlike] 预览失败: {e}", exc_info=True)
            return error_response(f"预览失败：{e}", status_code=500)

    async def api_get_state(self):
        try:
            return json_response(
                {"status": "ok", "data": {"sessions": self._session_brief(limit=60)}}
            )
        except Exception as e:
            logger.error(f"[humanlike] 读状态失败: {e}", exc_info=True)
            return error_response(f"读状态失败：{e}", status_code=500)

    async def api_write_state(self):
        try:
            payload = await web_request.json(default={}) or {}
        except Exception as e:
            return error_response(f"请求体解析失败：{e}")
        umo = str((payload or {}).get("umo") or "")
        action = str((payload or {}).get("action") or "")
        sess = (self._state.get("sessions") or {}).get(umo)
        if not isinstance(sess, dict):
            return error_response("没有这个会话")

        if action == "reset":
            self._emo_now(sess)  # 先把衰减结掉
            sess.pop("emo", None)
            sess.pop("emo_hold", None)
            sess["emo_ts"] = time.time()
        elif action == "soothe":
            self._bump_emo(sess, {"temper": 0.45, "spirits": 0.12}, 3600)
        elif action == "cheer":
            self._bump_emo(sess, {"energy": 0.35, "spirits": 0.25}, 3600)
        else:
            return error_response("未知操作")

        await self._save_state(force=True)
        emo = self._emo_now(sess)
        logger.info(f"[humanlike] 面板手动调整情绪 {umo} → {action}: {emo}")
        return json_response({"status": "ok", "data": {"emo": emo}})
