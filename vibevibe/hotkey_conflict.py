"""触发键冲突检查。

设错触发键的后果不会当场报错,只会表现为"按一下触发两次"或者
"按一下顺便干了件别的事",而且很难联想到是快捷键撞了。所以在
**设置的时候**就该拦住。

三类冲突,严重程度不同,处理方式也不同:

  blocker  这个键已经被 GNOME 快捷键占了 —— 按一下会同时触发两件事,
           直接拒绝

  blocker  这个键正是 vibevibe 自己那条 GNOME 快捷键 —— 两条通道
           (evdev 直读 + GNOME 快捷键)会同时收到,一次按键切换两次
           状态,等于什么都没发生。这个最坑,因为看起来像"没反应"

  warning  这个键在 X11 里有 keysym —— 不独占的情况下会漏给当前应用。
           绑 KEY_V 的话,每次触发听写都顺手输入一个 v。
           不拦,但要说清楚

推荐键**不能写死成"F13~F24"**:实测这台机器上 F13~F18 被 inet 布局
映射成了 XF86Tools / XF86Launch5~9,F20~F23 是音量和触摸板功能键,
真正空着的只有 F19 和 F24。所以推荐值由 suggest_free_keys() 按当前
布局现算,而不是印在文案里。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

# 合法 keysym 名的形状。GNOME 的加速器主键只会是这种 ASCII 标识符。
_VALID_KEY = re.compile(r"^[A-Za-z0-9_]+$")

# 要扫的 GNOME 快捷键 schema。这四个覆盖了窗口管理、媒体键、
# Shell 和 Mutter,是绝大多数系统级快捷键的所在。
GNOME_SCHEMAS = (
    "org.gnome.desktop.wm.keybindings",
    "org.gnome.settings-daemon.plugins.media-keys",
    "org.gnome.shell.keybindings",
    "org.gnome.mutter.keybindings",
)

CUSTOM_KEYBINDINGS_KEY = "org.gnome.settings-daemon.plugins.media-keys"
VIBEVIBE_CUSTOM_PATH = (
    "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/vibevibe/"
)

# evdev 修饰键名 → GNOME 加速器写法。GNOME 不区分左右。
MOD_TO_ACCEL = {
    "KEY_LEFTCTRL": "<Control>", "KEY_RIGHTCTRL": "<Control>",
    "KEY_LEFTALT": "<Alt>", "KEY_RIGHTALT": "<Alt>",
    "KEY_LEFTSHIFT": "<Shift>", "KEY_RIGHTSHIFT": "<Shift>",
    "KEY_LEFTMETA": "<Super>", "KEY_RIGHTMETA": "<Super>",
}

# evdev 主键名 → GNOME 加速器里的写法。只列会不一致的;
# 其余按"去掉 KEY_ 前缀"处理。
KEY_TO_ACCEL = {
    "KEY_ESC": "Escape", "KEY_ENTER": "Return", "KEY_SPACE": "space",
    "KEY_TAB": "Tab", "KEY_BACKSPACE": "BackSpace", "KEY_DELETE": "Delete",
    "KEY_PAUSE": "Pause", "KEY_INSERT": "Insert", "KEY_HOME": "Home",
    "KEY_END": "End", "KEY_PAGEUP": "Page_Up", "KEY_PAGEDOWN": "Page_Down",
    "KEY_UP": "Up", "KEY_DOWN": "Down", "KEY_LEFT": "Left", "KEY_RIGHT": "Right",
}


# GNOME 的加速器字符串**不是规范化的**:同一个组合可能写成
#   <Shift><Super>space  和  <Super><Shift>space   (修饰键顺序不同)
#   <Primary>d / <Ctrl>d / <Control>d              (同一个修饰键三种别名)
# 直接按字符串比对会漏掉冲突 —— 实测 <Shift><Super>space 已被
# 「切换输入法」占用,但按 <Super><Shift>space 去查会报"空闲"。
# 所以比对前一律先规范化成 (修饰键集合, 主键) 再比。
MOD_ALIASES = {
    "primary": "control", "ctrl": "control", "control": "control",
    "shift": "shift", "alt": "alt", "mod1": "alt",
    "super": "super", "meta": "super", "hyper": "super",
}


def normalize_accel(accel: str) -> tuple[frozenset[str], str] | None:
    """把 GNOME 加速器字符串规范化成 (修饰键集合, 主键小写)。

    认不出来的返回 None —— 那种情况**不能当成"没冲突"**,
    调用方要把它当作"这一项没检查到"。
    """
    accel = accel.strip().strip("'\"")
    if not accel:
        return None

    mods = set()
    rest = accel
    while rest.startswith("<"):
        end = rest.find(">")
        if end < 0:
            return None
        raw = rest[1:end].strip().lower()
        canon = MOD_ALIASES.get(raw)
        if canon is None:
            return None       # 不认识的修饰键,宁可说"没检查到"
        mods.add(canon)
        rest = rest[end + 1:]

    key = rest.strip()
    # keysym 名字只可能是 ASCII 字母数字和下划线(space / Page_Up / F19 / v)。
    # 不校验的话,随便一串中文也会被当成"合法且未被占用",
    # 于是给出虚假的"✓ 可用"。
    if not key or not _VALID_KEY.match(key):
        return None
    return frozenset(mods), key.lower()


@dataclass
class Conflict:
    """一条冲突。blocker=True 表示该拒绝设置,False 只是提醒。"""

    blocker: bool
    title: str
    detail: str


def _run(cmd: list[str]) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return p.stdout if p.returncode == 0 else ""
    except Exception:
        return ""


def to_accel(key_spec: str) -> str:
    """把 "KEY_LEFTCTRL+KEY_V" 转成 GNOME 的写法 "<Control>v"。

    转不出来时返回空串 —— 那种情况下就没法跟 GNOME 的绑定比对,
    只能跳过这一类检查(并且**不能**假装"没冲突")。
    """
    parts = [p.strip().upper() for p in key_spec.split("+") if p.strip()]
    if not parts:
        return ""

    mods = "".join(MOD_TO_ACCEL.get(p, "") for p in parts[:-1])
    if len(parts) > 1 and not mods:
        return ""

    main = parts[-1]
    if main in KEY_TO_ACCEL:
        return mods + KEY_TO_ACCEL[main]
    if not main.startswith("KEY_"):
        return ""
    bare = main[4:]
    if len(bare) == 1 and bare.isalpha():
        return mods + bare.lower()          # 字母键 GNOME 用小写
    if bare.startswith("F") and bare[1:].isdigit():
        return mods + bare                  # F19 保持原样
    if bare.isdigit():
        return mods + bare
    return ""


def x11_keysym(key_spec: str) -> str:
    """这个键在当前 X11 布局下的 keysym。空串 = 没有 keysym。

    没有 keysym 意味着**漏给应用也不会有任何反应** —— 这正是
    F13~F24 适合当触发键的原因。
    """
    parts = [p.strip().upper() for p in key_spec.split("+") if p.strip()]
    if not parts:
        return ""
    try:
        from evdev import ecodes

        code = ecodes.ecodes.get(parts[-1])
    except ImportError:
        return ""
    if code is None:
        return ""

    # X11 keycode = evdev code + 8,这是内核和 X 之间的固定偏移
    target = code + 8
    for line in _run(["xmodmap", "-pke"]).splitlines():
        head, _, tail = line.partition("=")
        bits = head.split()
        if len(bits) < 2 or not bits[1].isdigit():
            continue
        if int(bits[1]) != target:
            continue
        syms = tail.split()
        return syms[0] if syms else ""
    return ""


def gnome_bindings() -> dict[tuple[frozenset[str], str], list[str]]:
    """扫出 GNOME 里所有已绑定的快捷键。

    键是**规范化后**的 (修饰键集合, 主键),不是原始字符串 ——
    见 normalize_accel 的说明。
    """
    used: dict[tuple[frozenset[str], str], list[str]] = {}

    def add(accel: str, who: str) -> None:
        accel = accel.strip().strip("'\"")
        if not accel or accel == "@as []":
            return
        norm = normalize_accel(accel)
        if norm is None:
            return
        used.setdefault(norm, []).append(f"{who} [{accel}]")

    for schema in GNOME_SCHEMAS:
        for line in _run(["gsettings", "list-recursively", schema]).splitlines():
            bits = line.split(None, 2)
            if len(bits) < 3:
                continue
            _, key, value = bits
            if not value.startswith("["):
                continue
            for item in value.strip("[]").split(","):
                add(item, f"{schema.split('.')[-1]}.{key}")

    # 自定义快捷键单独存在各自的路径下,list-recursively 扫不到
    paths = _run(["gsettings", "get", CUSTOM_KEYBINDINGS_KEY, "custom-keybindings"])
    for path in paths.strip().strip("@as []").split(","):
        path = path.strip().strip("'\"")
        if not path:
            continue
        schema = f"{CUSTOM_KEYBINDINGS_KEY}.custom-keybinding:{path}"
        binding = _run(["gsettings", "get", schema, "binding"]).strip()
        name = _run(["gsettings", "get", schema, "name"]).strip().strip("'\"")
        is_ours = path == VIBEVIBE_CUSTOM_PATH
        add(binding, f"自定义快捷键「{name}」" + ("  ← vibevibe 自己的" if is_ours else ""))

    return used


# 候选触发键。F13~F24 在物理键盘上不存在,所以最不容易撞车 ——
# 但**具体哪几个真的空着要看当前布局**,不能一概而论。
CANDIDATE_KEYS = tuple(f"KEY_F{i}" for i in range(13, 25))


def suggest_free_keys(limit: int = 6) -> list[str]:
    """在当前 X11 布局下,哪些候选键真的没有 keysym。

    为什么要现算而不是写死一句「推荐 F13~F24」:实测这台机器上
    F13~F18 被 inet 布局映射成了 XF86Tools / XF86Launch5~9,
    只有 F19 和 F24 是空的。一句笼统的推荐会把人引到有 keysym 的键上。
    """
    free = []
    for name in CANDIDATE_KEYS:
        if not x11_keysym(name):
            free.append(name)
        if len(free) >= limit:
            break
    return free


def check(key_spec: str) -> list[Conflict]:
    """检查一个触发键有没有冲突。返回的列表为空 = 可以放心用。"""
    out: list[Conflict] = []
    accel = to_accel(key_spec)

    # ── 1. 跟 GNOME 快捷键撞车 ──
    if accel:
        used = gnome_bindings()
        norm = normalize_accel(accel)
        owners = used.get(norm, []) if norm else []
        ours = [o for o in owners if "vibevibe 自己的" in o]
        others = [o for o in owners if "vibevibe 自己的" not in o]

        if ours:
            out.append(Conflict(
                blocker=True,
                title=f"{key_spec} 已经是 vibevibe 自己的 GNOME 快捷键",
                detail=(
                    "两条通道会同时收到这个键:evdev 直读一次、GNOME 快捷键一次。"
                    "toggle 模式下等于连切两次状态,表现为「按了没反应」,"
                    "而且极难联想到原因。\n"
                    "要么换一个键,要么先把那条 GNOME 快捷键删掉。"),
            ))
        if others:
            out.append(Conflict(
                blocker=True,
                title=f"{key_spec}({accel})已被系统快捷键占用",
                detail="占用它的是:\n  " + "\n  ".join(others)
                       + "\n按一下会同时触发听写和那个功能。",
            ))
    elif "+" in key_spec:
        out.append(Conflict(
            blocker=False,
            title="没法跟 GNOME 快捷键比对",
            detail=f"{key_spec} 转不成 GNOME 的加速器写法,"
                   "所以「有没有跟系统快捷键撞车」这一项没检查到。",
        ))

    # ── 2. 会不会漏给当前应用 ──
    sym = x11_keysym(key_spec)
    if sym:
        free = suggest_free_keys()
        tip = ("换成这几个键之一就没这个问题(当前布局下它们没有 keysym):\n  "
               + ", ".join(free)) if free else \
              "当前布局下候选键 F13~F24 全都有 keysym,只能接受这个副作用。"
        out.append(Conflict(
            blocker=False,
            title=f"这个键在 X11 里有 keysym({sym}),会漏给当前应用",
            detail=(
                "因为不能独占设备(独占会把同一个小键盘上其它键也吞掉),"
                f"按下时当前窗口也会收到 {sym}。\n" + tip),
        ))

    return out


def describe(key_spec: str) -> str:
    """给命令行用的一句话结论。"""
    issues = check(key_spec)
    if not issues:
        sym = x11_keysym(key_spec)
        return f"✓ {key_spec} 没有冲突" + ("" if sym else "(且无 keysym,不会漏给应用)")
    worst = "✗" if any(c.blocker for c in issues) else "!"
    return f"{worst} {key_spec}: " + "; ".join(c.title for c in issues)


# ── 绑定 / 解绑 GNOME 自定义快捷键 ──────────────────────────────────
#
# setup 向导和设置界面都要改这条绑定,逻辑放这儿共用,免得两处各写一遍
# 然后慢慢跑偏。

def _gsettings_set(schema: str, key: str, value: str) -> bool:
    try:
        p = subprocess.run(["gsettings", "set", schema, key, value],
                           capture_output=True, text=True, timeout=15)
        return p.returncode == 0
    except Exception:
        return False


def bind_shortcut(accel: str, command: str, name: str = "vibevibe 语音听写") -> bool:
    """把 vibevibe 那条自定义快捷键绑到 accel 上(已存在就改绑)。

    会**保留用户其它的自定义快捷键** —— 直接覆盖 custom-keybindings
    列表会把别人的绑定一起抹掉。
    """
    existing_raw = _run(["gsettings", "get", CUSTOM_KEYBINDINGS_KEY,
                         "custom-keybindings"]).strip()
    paths = []
    if existing_raw and existing_raw not in ("@as []", "[]"):
        paths = [x.strip().strip("'\"")
                 for x in existing_raw.lstrip("@as ").strip("[]").split(",")
                 if x.strip()]
    if VIBEVIBE_CUSTOM_PATH not in paths:
        paths.append(VIBEVIBE_CUSTOM_PATH)

    value = "[" + ", ".join(f"'{p}'" for p in paths) + "]"
    schema = f"{CUSTOM_KEYBINDINGS_KEY}.custom-keybinding:{VIBEVIBE_CUSTOM_PATH}"
    return all([
        _gsettings_set(CUSTOM_KEYBINDINGS_KEY, "custom-keybindings", value),
        _gsettings_set(schema, "name", name),
        _gsettings_set(schema, "command", command),
        _gsettings_set(schema, "binding", accel),
    ])


def current_shortcut() -> str:
    """当前 vibevibe 那条自定义快捷键绑的是什么。没绑返回空串。"""
    schema = f"{CUSTOM_KEYBINDINGS_KEY}.custom-keybinding:{VIBEVIBE_CUSTOM_PATH}"
    return _run(["gsettings", "get", schema, "binding"]).strip().strip("'\"")


def check_accel(accel: str) -> list[Conflict]:
    """检查一个 GNOME 加速器有没有被别人占。

    跟 check() 的区别:那个吃的是 evdev 键名(KEY_F19),这个吃的是
    GNOME 写法(<Super><Shift>v)。桌面快捷键这条通道由 GNOME 抢占,
    不会漏给应用,所以**不需要查 keysym 那一项**。
    """
    out: list[Conflict] = []
    norm = normalize_accel(accel)
    if norm is None:
        out.append(Conflict(
            blocker=True, title=f"{accel} 不是合法的快捷键写法",
            detail="应该形如 <Super><Shift>v 或 <Control><Alt>space。"))
        return out

    owners = [o for o in gnome_bindings().get(norm, [])
              if "vibevibe 自己的" not in o]
    if owners:
        out.append(Conflict(
            blocker=True, title=f"{accel} 已被占用",
            detail="占用它的是:\n  " + "\n  ".join(owners)))
    return out
