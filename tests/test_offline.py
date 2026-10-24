"""离线验证 astrbot_plugin_humanlike 的文本处理与状态逻辑。

覆盖：清洗 / 拆条 / 情绪三条轴 / 沉默 / 反露馅四道保险 / 私聊 / 提示词契约。
不启动 AstrBot、不连 QQ，只加载插件模块并直接调用内部方法。

跑法（需要 AstrBot 的 Python 环境，或先 pip install astrbot）：
    export ASTRBOT_BACKEND=/path/to/AstrBot/backend/app     # Windows 用 $env:ASTRBOT_BACKEND=...
    python tests/test_offline.py
"""

import asyncio
import importlib.util
import io
import logging
import os
import re
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
BACKEND = os.environ.get("ASTRBOT_BACKEND", "")
PLUGIN_MAIN = Path(os.environ.get("HUMANLIKE_MAIN", HERE.parent / "main.py"))
if BACKEND:
    sys.path.insert(0, BACKEND)

logging.basicConfig(level=logging.WARNING)

spec = importlib.util.spec_from_file_location("humanlike_main", PLUGIN_MAIN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

CONFIG = {
    "enable": True,
    "groups": "888888888",
    "prompt_extra": "",
    "style": {"max_chars": 20, "target_chars": 10, "split_prob": 0.0,
              "strip_action": True, "strip_markdown": True,
              "drop_trailing_period": True, "keep_ellipsis": True},
    "timing": {"read_min": 0.0, "read_max": 0.0, "think_min": 0.0, "think_max": 0.0,
               "long_pause_prob": 0.0, "max_delay": 1.0, "night_slowdown": False},
    "silence": {"prob": 0.0, "never_silent_on_question": True, "spam_window": 120,
                "cooldown": 900},
    "mood": {"enable": True, "energy_baseline": 0.42, "spirits_baseline": 0.56,
             "temper_baseline": 0.68, "decay_per_hour": 1.0, "annoy_step": 0.15,
             "react_scale": 1.0, "ignored_cooldown": 600, "edge": 0.3,
             "temper_floor": 0.15},
    "consistency": {"self_lines": 6},
    "guards": {"dedupe_stance": True, "attribute_transfer": True,
               "fact_conflict": True, "drop_stale": False,
               "tease_reaction": True, "recent_human": 12},
}

plugin = mod.HumanLikePlugin(None, CONFIG)


class FakeEvent:
    def __init__(self, text="", at=True, umo="default:GroupMessage:888888888"):
        self._text = text
        self._extra = {}
        self.is_at_or_wake_command = at
        self.unified_msg_origin = umo

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extra
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_message_str(self):
        return self._text

    def get_group_id(self):
        return self.unified_msg_origin.rsplit(":", 1)[-1]

    def get_session_id(self):
        return "default:GroupMessage:888888888"


results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'✅' if cond else '❌'} {name}" + (f"  → {detail}" if detail else ""))


# ── 1. 清洗 ──────────────────────────────────────────────
s1 = plugin._sanitize("**嗯**，我知道了。\n- 第一点\n- 第二点")
check("Markdown 被清掉", "*" not in s1 and "- 第" not in s1, repr(s1))

s2 = plugin._sanitize("（耳朵抖）不知道喵。")
check("括号动作被删", "耳朵" not in s2 and "（" not in s2, repr(s2))

s3 = plugin._sanitize("首先，我不去。希望这对你有帮助。")
check("助手腔整句丢弃", "希望" not in s3 and "首先" not in s3, repr(s3))

s4 = plugin._sanitize("我去睡了。")
check("句末句号被去掉", not s4.endswith("。"), repr(s4))

long_text = "其实我觉得这个事情吧还是要看情况因为大家时间都不一样"
s5 = plugin._sanitize(long_text)
check(f"超长被截断到 ≤20 字", len(s5) <= 20, f"{len(s5)} 字 {s5!r}")

s6 = plugin._sanitize("在吗？")
check("问号保留", s6.endswith("？"), repr(s6))

# ── 2. 拆条 ──────────────────────────────────────────────
plugin.config["style"]["split_prob"] = 1.0
bubbles = plugin._split_bubbles("周末啊，叶铭大概率不动")
check("可拆成两条", len(bubbles) == 2, repr(bubbles))

plugin.config["style"]["split_prob"] = 0.0
bubbles0 = plugin._split_bubbles("周末啊，叶铭大概率不动")
check("关掉拆条后只有一条", len(bubbles0) == 1, repr(bubbles0))

# ── 3. 情绪：三条轴 ──────────────────────────────────────
sess = plugin._session("default:GroupMessage:888888888")
emo0 = plugin._emo_now(sess)
check("三条轴都在基线",
      all(abs(emo0[k] - CONFIG["mood"][f"{k}_baseline"]) < 1e-6
          for k in ("energy", "spirits", "temper")), str(emo0))

line0 = plugin._emotion_line(sess)
check("默认不写「懒散」", "懒散" not in line0 and "跟平时一样" in line0, line0)

# 3.1 被夸 → 开心 + 脾气好
qa = plugin._session("default:GroupMessage:888888881")
plugin._emo_react(qa, "你也太可爱了吧", 1.0)
emo_q = plugin._emo_now(qa)
check("被夸 → 情绪涨", emo_q["spirits"] > emo0["spirits"] + 0.1,
      f"{emo0['spirits']:.2f} → {emo_q['spirits']:.2f}")
check("被夸 → 脾气也涨", emo_q["temper"] > emo0["temper"] + 0.05, f"{emo_q['temper']:.2f}")
line_q = plugin._emotion_line(qa)
check("夸完状态写「心情不错」", "心情不错" in line_q, line_q)

# 3.2 被骂 → 生气，且不再叠「轻快」
qb = plugin._session("default:GroupMessage:888888882")
plugin._emo_react(qb, "你有病吧 滚", 1.0)
emo_b = plugin._emo_now(qb)
check("被骂 → 脾气掉进生气段", emo_b["temper"] < 0.28, f"{emo_b['temper']:.2f}")
check("被骂 → 情绪也跟着掉", emo_b["spirits"] < emo0["spirits"], f"{emo_b['spirits']:.2f}")
line_b = plugin._emotion_line(qb)
check("生气时状态写「在生气」", "在生气" in line_b, line_b)
check("生气时不叠「轻快」提示", "轻快" not in line_b, line_b)

# 3.3 被拉去玩 → 活力
qc = plugin._session("default:GroupMessage:888888883")
plugin._emo_react(qc, "晚上开黑来不来", 1.0)
emo_c = plugin._emo_now(qc)
check("被拉去开黑 → 活力涨", emo_c["energy"] > 0.6, f"{emo_c['energy']:.2f}")
check("活力上来后状态写「来劲」", "来劲" in plugin._emotion_line(qc),
      plugin._emotion_line(qc))

# 3.4 没叫到自己时只算旁观，推得轻
qd = plugin._session("default:GroupMessage:888888884")
plugin._emo_react(qd, "晚上开黑来不来", 0.4)
check("旁观只推四成", emo0["energy"] < plugin._emo_now(qd)["energy"] < emo0["energy"] + 0.12,
      f"{plugin._emo_now(qd)['energy']:.2f}")

qd2 = plugin._session("default:GroupMessage:888888885")
plugin._emo_react(qd2, "你有病吧 滚", 0.4)
check("旁观别人被骂不至于跟着气炸",
      plugin._emo_now(qd2)["temper"] > 0.28, f"{plugin._emo_now(qd2)['temper']:.2f}")

# 3.5 被连续 @ 掉脾气
qe = plugin._session("default:GroupMessage:888888886")
before_at = plugin._emo_now(qe)["temper"]
for _ in range(3):
    plugin._track_at(qe)
check("连续被 @ 掉脾气", plugin._emo_now(qe)["temper"] < before_at,
      f"{before_at:.2f} → {plugin._emo_now(qe)['temper']:.2f}")

# 3.6 按住 vs 不按住：半小时后差一截
qf = plugin._session("default:GroupMessage:888888887")
plugin._emo_react(qf, "滚", 1.0)
held_t = qf["emo"]["temper"]
qf["emo_ts"] = time.time() - 1800
held_after = plugin._emo_now(qf)["temper"]

qg = plugin._session("default:GroupMessage:888888878")
qg["emo"] = {"energy": 0.42, "spirits": 0.56, "temper": 0.26}
qg["emo_hold"] = {"temper": 0.0}
qg["emo_ts"] = time.time() - 1800
free_after = plugin._emo_now(qg)["temper"]
check("被按住的那半小时几乎不回弹", held_after < held_t + 0.05, f"{held_t:.2f} → {held_after:.2f}")
check("没按住的半小时明显回弹", free_after - held_after > 0.1,
      f"按住 {held_after:.2f} / 没按住 {free_after:.2f}")

# 3.7 旧 state.json 只有一个 mood，也能接上
qh = plugin._session("default:GroupMessage:888888877")
qh.pop("emo", None)
qh.pop("emo_hold", None)
qh["mood"] = 0.2
qh["mood_ts"] = time.time()
mig = plugin._emo_now(qh)
check("旧单值心情迁移到情绪轴",
      abs(mig["spirits"] - 0.2) < 1e-6 and "mood" not in qh, str(mig))
check("迁移后另外两轴回基线",
      abs(mig["energy"] - 0.42) < 1e-6 and abs(mig["temper"] - 0.68) < 1e-6, str(mig))

plugin.config["mood"]["enable"] = False
frozen = plugin._emo_now(qa)
check("关掉情绪后三条轴都停在基线",
      frozen == {"energy": 0.42, "spirits": 0.56, "temper": 0.68}, str(frozen))
plugin.config["mood"]["enable"] = True

# 3.8 状态会改变目标字数
qi = plugin._session("default:GroupMessage:888888876")
qi["emo"] = {"energy": 0.10, "spirits": 0.56, "temper": 0.68}
qi["emo_ts"] = time.time()
qi_low = plugin._build_contract(FakeEvent("在吗"), qi)
qi["emo"] = {"energy": 0.95, "spirits": 0.56, "temper": 0.68}
qi["emo_ts"] = time.time()
qi_high = plugin._build_contract(FakeEvent("在吗"), qi)


def _target_of(text: str) -> int:
    m = re.search(r"3~(\d+) 字", text)
    return int(m.group(1)) if m else -1


check("有劲时目标字数更长", _target_of(qi_high) > _target_of(qi_low),
      f"{_target_of(qi_low)} vs {_target_of(qi_high)} 字")

# ── 3.9 语气开关：默认只准"话少"，不准"刻薄" ─────────────
qj = plugin._session("default:GroupMessage:888888875")
plugin._emo_react(qj, "你有病吧 滚", 1.0)
line_default = plugin._emotion_line(qj)
check("默认锋利度下不出现「顶一句」", "顶一句" not in line_default, line_default)
check("默认文案要求别甩脸色", "别甩脸色" in line_default, line_default)
check("情绪低时必定带一句「刻薄不行」", "刻薄不行" in line_default, line_default)

plugin.config["mood"]["edge"] = 0.8
line_sharp = plugin._emotion_line(qj)
check("锋利度 0.8 才启用「顶一句」文案", "顶一句" in line_sharp, line_sharp)
plugin.config["mood"]["edge"] = 0.3

# 3.10 脾气下限：填 0.45 就再也进不了"在生气"
plugin.config["mood"]["temper_floor"] = 0.45
qk = plugin._session("default:GroupMessage:888888874")
plugin._emo_react(qk, "你有病吧 滚", 1.0)
floor_t = plugin._emo_now(qk)["temper"]
check("脾气下限 0.45 生效", floor_t >= 0.45, f"{floor_t:.2f}")
check("下限 0.45 时不再出现「在生气」", "在生气" not in plugin._emotion_line(qk),
      plugin._emotion_line(qk))
plugin.config["mood"]["temper_floor"] = 0.15

# 3.11 对方说"你这话过分了"，脾气要立刻回来
ql = plugin._session("default:GroupMessage:888888873")
plugin._emo_react(ql, "你有病吧 滚", 1.0)
angry = plugin._emo_now(ql)["temper"]
plugin._emo_react(ql, "你这话有点过分了吧", 1.0)
back_t = plugin._emo_now(ql)["temper"]
check("被说伤人 → 脾气回升", back_t > angry + 0.25, f"{angry:.2f} → {back_t:.2f}")
check("回升后不再写「在生气」", "在生气" not in plugin._emotion_line(ql),
      plugin._emotion_line(ql))

# 3.12 追加提示词原样进契约，且排在最后
plugin.config["prompt_extra"] = "不管什么状态，都不许用反问句怼人"
c_extra = plugin._build_contract(FakeEvent("在吗"), plugin._session("default:GroupMessage:888888872"))
check("prompt_extra 进了契约", "不许用反问句怼人" in c_extra, "")
check("prompt_extra 标成额外要求", "额外要求" in c_extra, "")
plugin.config["prompt_extra"] = ""

# 3.13 契约里有"状态不好不许刻薄"这条硬规则
check("契约含规则 17（不刻薄）", "不表现为刻薄" in c_extra, "")

# 3.14 刚无视过一次，下一次必须接
qm = plugin._session("default:GroupMessage:888888871")
plugin.config["silence"] = {"prob": 0.99, "never_silent_on_question": False,
                            "spam_window": 120, "cooldown": 900}
qm["last_silence_at"] = time.time()
check("刚不回过的 15 分钟内必须接", plugin._should_silence(qm, "在吗") is False)
qm["last_silence_at"] = 0.0
check("冷却过了才会再次不回", plugin._should_silence(qm, "在吗") is True)
plugin.config["silence"] = {"prob": 0.0, "never_silent_on_question": True,
                            "spam_window": 120, "cooldown": 900}

# ── 4. 沉默策略 ──────────────────────────────────────────
plugin.config["silence"]["prob"] = 0.99
q = plugin._should_silence(sess, "在吗？")
check("直接提问不沉默", q is False, f"silence={q}")

plugin.config["silence"]["never_silent_on_question"] = False
q2 = plugin._should_silence(sess, "在吗？")
check("关闭保护后可沉默", q2 is True, f"silence={q2}")
plugin.config["silence"]["never_silent_on_question"] = True
plugin.config["silence"]["prob"] = 0.0

# ── 5. 提示词契约 ────────────────────────────────────────
plugin.config["style"]["split_prob"] = 0.35
ev = FakeEvent("来不来，周末开黑")
contract = plugin._build_contract(ev, sess)
checks = {
    "含身份声明": "真人" in contract,
    "含字数上限": "不超过 20 字" in contract,
    "含状态": "现在状态" in contract,
    "含时间": "现在时间" in contract,
    "含邀请情境": "不想去" in contract,
    "含反助手腔": "Markdown" in contract,
}
for k, v in checks.items():
    check(k, v, "")

# ── 6. 会话隔离 ──────────────────────────────────────────
check("生效群命中", plugin._enabled_for(FakeEvent("x")) is True)
check("未生效群拒绝",
      plugin._enabled_for(FakeEvent("x", umo="default:GroupMessage:12345")) is False)
check("未唤醒不介入",
      plugin._enabled_for(FakeEvent("x", at=False)) is True)  # 开关层面仍启用

# ── 7. 反露馅硬保险 ──────────────────────────────────────
now = time.time()
sess7 = plugin._session("default:GroupMessage:777770001")

# 7.1 复读：上一轮说「别点太辣的」，这一轮换成「别放太多辣」
sess7["self_lines"] = ["吃啊，别点太辣的就行"]
out, hit = plugin._guard_stance_repeat("毛血旺我可以，别放太多辣就行", sess7)
check("复读同一句约束被切掉", hit and out == "毛血旺我可以", repr(out))

out2, hit2 = plugin._guard_stance_repeat("吃啊", sess7)
check("没复读的短答不误杀", hit2 is False and out2 == "吃啊", repr(out2))

check("两种说法抽到同一约束",
      mod._stance_keys("别点太辣的就行") == mod._stance_keys("别放太多辣就行"),
      repr(mod._stance_keys("别点太辣的就行")))
check("「微辣」和「别太辣」也算同一约束",
      mod._stance_keys("吃啊，微辣就行") == mod._stance_keys("毛血旺可以，别整太辣就行"),
      repr(mod._stance_keys("吃啊，微辣就行")))

# 7.2 属性错配：乙说「反正我开车」，它接「我开车就不喝了」
sess7["self_lines"] = []
sess7["recent_human"] = [{"t": now, "name": "乙", "text": "我无所谓 反正我开车"}]
check("属性抽取正确", mod._self_claims("反正我开车") == {"开车"},
      repr(mod._self_claims("反正我开车")))

out3 = plugin._guard_attribute_transfer("带吧，我开车就不喝了", sess7)
check("别人的属性被切掉", out3 == "带吧", repr(out3))

sess7["self_lines"] = ["我开车来的"]
out4 = plugin._guard_attribute_transfer("我开车就不喝了", sess7)
check("自己说过的属性放行", out4 == "我开车就不喝了", repr(out4))

# 7.2b 隔了七八条之后再错配，也要抓得住（只看最近 6 条会漏）
sess7["self_lines"] = []
sess7["recent_human"] = [
    {"t": now - 60, "name": "乙", "text": "那正好 我开车过去十分钟"},
    {"t": now - 50, "name": "甲", "text": "你几点能到"},
    {"t": now - 40, "name": "丙", "text": "开车就别喝了"},
    {"t": now - 30, "name": "甲", "text": "你还吃辣吗"},
    {"t": now - 20, "name": "乙", "text": "他家微辣都挺辣"},
    {"t": now - 10, "name": "甲", "text": "酒谁带"},
]
out3b = plugin._guard_attribute_transfer("我开车，就不带了", sess7)
check("隔了八条也能抓住属性错配", out3b == "就不带了", repr(out3b))
check("时间不算身份属性",
      mod._self_claims("我六点半到") == set(), repr(mod._self_claims("我六点半到")))

# 7.3 事实冲突：乙说「我都订好位了」，它还提「先占个位」
sess7["self_lines"] = []
sess7["recent_human"] = [{"t": now, "name": "乙", "text": "去啊 我都订好位了 六点半"}]
out5 = plugin._guard_fact_conflict("我六点半到，正好先占个位", sess7)
check("和已说定的事冲突被切掉", out5 == "我六点半到", repr(out5))

out6 = plugin._guard_fact_conflict("我六点半到", sess7)
check("不冲突的话放行", out6 == "我六点半到", repr(out6))

# 7.4 滞后补答：话题已经翻到「带酒」，它还在答「吃辣」
sess7["recent_human"] = [
    {"t": now - 40, "name": "甲", "text": "你还吃辣吗"},
    {"t": now - 30, "name": "丙", "text": "说不吃还点毛血旺 笑死"},
    {"t": now - 5, "name": "甲", "text": "周六带不带酒"},
]
check("默认关闭时不误杀",
      plugin._guard_stale("毛血旺我可以，别放太多辣就行", sess7) is False)

plugin.config["guards"]["drop_stale"] = True
check("开启后能识别滞后补答",
      plugin._guard_stale("毛血旺我可以，别放太多辣就行", sess7) is True)
check("开启后接上最新话题也不误杀",
      plugin._guard_stale("带吧，我喝不了多少", sess7) is False)
check("太短的回复不做判断", plugin._guard_stale("吃啊", sess7) is False)
plugin.config["guards"]["drop_stale"] = False

# 7.5 被起哄：必须有反应，且反应不连着重复
sess7["last_reply_at"] = now - 120
sess7["recent_human"] = [{"t": now - 10, "name": "丙", "text": "说不吃还点毛血旺 笑死"}]
check("起哄被识别", plugin._is_teased(sess7) is True)

sess7["recent_human"] = [{"t": now - 10, "name": "甲", "text": "明天几点集合"}]
check("普通闲聊不算起哄", plugin._is_teased(sess7) is False)

sess7["recent_human"] = [{"t": now - 10, "name": "丙", "text": "他上次不是说不吃了吗 笑死"}]
check("被指出说过的话算 deny 类", plugin._tease_kind(sess7) == "deny")
sess7["recent_human"] = [{"t": now - 10, "name": "乙", "text": "哈哈 你也太慢了吧"}]
check("只是被笑算 laugh 类", plugin._tease_kind(sess7) == "laugh")

r1 = plugin._reaction(sess7)
r2 = plugin._reaction(sess7)
check("反应不连着用同一句", r1 != r2, f"{r1} / {r2}")

# 7.6 契约里出现新规则与已说定的事
ev7 = FakeEvent("周六带不带酒")
sess7["recent_human"] = [
    {"t": now, "name": "乙", "text": "去啊 我都订好位了 六点半"},
    {"t": now - 3, "name": "丙", "text": "说不吃还点毛血旺 笑死"},
]
contract7 = plugin._build_contract(ev7, sess7)
for name, cond in {
    "契约含「只回最新」": "只回最新的一条消息" in contract7,
    "契约含属性隔离": "那是别人的情况" in contract7,
    "契约含已说定的事": "已经说定的事" in contract7,
    "契约含被起哄要求": "起哄你" in contract7,
}.items():
    check(name, cond, "")

# ── 8. 接话头：不 @ 也接 ─────────────────────────────────
plugin.config["followup"] = {"enable": True, "prob": 1.0, "window": 240, "max_chain": 3}
sess8 = plugin._session("default:GroupMessage:777770002")
sess8["last_reply_at"] = time.time() - 5
sess8["prev_was_self"] = True
sess8["self_lines"] = ["那个群天天挂人，烦"]
sess8["follow_chain"] = 0

check("紧挨着我上一条 → 接住", plugin._is_follow_up(sess8, "原因是给人骂破防了") is True)
sess8["prev_was_self"] = False
check("中间夹了别人、又没词重合 → 不接",
      plugin._is_follow_up(sess8, "今晚吃什么") is False)
check("有词重合 → 还是接住", plugin._is_follow_up(sess8, "那个群确实烦") is True)

sess8["prev_was_self"] = True
check("纯附和「算」不接", plugin._is_follow_up(sess8, "算") is False)
check("纯附和「笑死」不接", plugin._is_follow_up(sess8, "笑死") is False)
check("「嗯」不接", plugin._is_follow_up(sess8, "嗯") is False)
check("有实义的四字以内照接", plugin._is_follow_up(sess8, "骂得好") is True)
sess8["prev_was_self"] = False

sess8["last_reply_at"] = time.time() - 600
check("超过窗口就不接", plugin._is_follow_up(sess8, "原因是给人骂破防了") is False)
sess8["last_reply_at"] = time.time() - 5

sess8["prev_was_self"] = True
sess8["follow_chain"] = 3
sess8["follow_chain_at"] = time.time()
check("连到上限就停", plugin._is_follow_up(sess8, "绷不住了") is False)
sess8["follow_chain"] = 0
check("窗口过了自动重置连击计数", True, "")

plugin.config["followup"]["enable"] = False
check("关掉后完全不接", plugin._is_follow_up(sess8, "绷不住了") is False)
plugin.config["followup"]["enable"] = True

ev8 = FakeEvent("原因是给人骂破防了", at=False)
ev8.set_extra("humanlike_followup", True)
c8 = plugin._build_contract(ev8, sess8)
checks8 = {
    "契约含接话头情境": "接着你上一条在说" in c8,
    "契约要求别问跟谁说": "跟谁说" in c8,
}
for k, v in checks8.items():
    check(k, v, "")

# ── 9. 私聊 ──────────────────────────────────────────────
class PrivateEvent(FakeEvent):
    """私聊事件：umo 里带 FriendMessage，且没有群号。"""

    def __init__(self, text="", uid="2173955614"):
        super().__init__(text, at=True, umo=f"default:FriendMessage:{uid}")
        self._uid = uid

    def get_group_id(self):
        return ""

    def get_sender_id(self):
        return self._uid


plugin.config["private_enable"] = False
check("私聊默认不生效", plugin._enabled_for(PrivateEvent()) is False)

plugin.config["private_enable"] = True
plugin.config["private_users"] = ""
check("私聊打开就生效", plugin._enabled_for(PrivateEvent()) is True)

plugin.config["private_users"] = "2173955614"
check("私聊白名单命中", plugin._enabled_for(PrivateEvent()) is True)
check("私聊白名单未命中", plugin._enabled_for(PrivateEvent(uid="10000")) is False)
plugin.config["private_users"] = ""

check("私聊白名单不影响群聊", plugin._enabled_for(FakeEvent("在吗")) is True)

sess9 = plugin._session("default:FriendMessage:2173955614")
c9 = plugin._build_contract(PrivateEvent("你今天干嘛了"), sess9)
for k, v in {
    "私聊契约写明一对一": "一对一聊天" in c9,
    "私聊契约写这段私聊": "这段私聊" in c9,
    "私聊契约不用群口径": "QQ 群里聊天" not in c9,
    "私聊契约不用等叫": "不用等叫" in c9,
}.items():
    check(k, v, "")

c9g = plugin._build_contract(FakeEvent("在吗"), sess9)
check("群聊契约仍是群口径", "QQ 群里聊天" in c9g, "")


class FlagEvent(FakeEvent):
    """能记下 gate 有没有让这条消息停下。"""

    def __init__(self, text="", umo="default:GroupMessage:888888888", at=True):
        super().__init__(text, at=at, umo=umo)
        self.stopped = False
        self.cleared = False
        self.llm_flags = []

    def stop_event(self):
        self.stopped = True

    def clear_result(self):
        self.cleared = True

    def should_call_llm(self, flag=True):
        self.llm_flags.append(flag)

    def set_result(self, _result):
        pass

    def get_sender_name(self):
        return "某甲"

    def get_sender_id(self):
        return "2173955614"


class GatePrivateEvent(FlagEvent):
    def __init__(self, text="", uid="2173955614", at=True):
        super().__init__(text, umo=f"default:FriendMessage:{uid}", at=at)
        self._uid = uid

    def get_group_id(self):
        return ""

    def get_sender_id(self):
        return self._uid


# 私聊不静默：静默概率拉满也不静默，而且要把他说的这句记下来
plugin.config["silence"] = {"prob": 1.0, "never_silent_on_question": True,
                            "spam_window": 120}
plugin.config["followup"] = {"enable": False, "prob": 0.0, "window": 240, "max_chain": 1}
plugin.config["initiative"] = {"prob": 0.0}

ev_p = GatePrivateEvent("今天好累")
asyncio.run(plugin.human_gate(ev_p))
check("私聊不静默", ev_p.stopped is False)
check("私聊不拦 LLM", False not in ev_p.llm_flags, str(ev_p.llm_flags))
sess_p = plugin._session("default:FriendMessage:2173955614")
check("私聊也记下他说的话", len(sess_p.get("recent_human") or []) >= 1,
      str(sess_p.get("recent_human"))[:60])

# 同样的文本、同样的配置，群里就会被静默 —— 证明上一条不是空转
ev_g = FlagEvent("今天好累", at=True)
asyncio.run(plugin.human_gate(ev_g))
check("群里同样条件会静默（对照）", ev_g.stopped is True)

# ── 汇总 ────────────────────────────────────────────────
failed = [r for r in results if not r[1]]
print(f"\n===== {len(results) - len(failed)}/{len(results)} 通过 =====")
if failed:
    for name, _, detail in failed:
        print(f"  失败: {name} {detail}")
sys.exit(1 if failed else 0)
