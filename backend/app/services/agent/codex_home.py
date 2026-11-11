"""Codex 核心的隔离配置目录（`CODEX_HOME`）。

为什么需要它
------------
Codex CLI 默认从 `~/.codex` 读**用户自己的** AGENTS.md、config.toml、hooks、
memories、人格设定、登录态和历史会话。个人 Agent 直接用用户的 home，就会把用户
长期使用的开发规范（例如「每次回复加御主」「按 Java 项目约定」）当成本应用的系统
提示词——用户自己那次对话里模型答非所问，根因就在这里。

所以应用给 Codex 一个自己的配置目录：

- 目录在数据目录下（`<数据目录>/agent_codex_home`），持久、应用独占，所有会话共用；
- 目录里只有应用写的一份 `config.toml`（说明性注释 + 无任何个性化配置）；
- 模型与供应商不写进配置文件，而是每轮由运行器用 `-c` 参数注入，换模型不影响这里；
- 用户的 Codex 技能以**目录联接/符号链接**只读挂到 `skills/` 下，技能可以复用，
  但内容不会被复制进仓库，也不会被改写；
- 绝不读取也不改写用户 `~/.codex` 下的其它任何东西。

挂技能为什么用链接而不是 `[[skills.config]]`：在 codex-cli 0.167.0 上实测，
`[[skills.config]]` 里指向 CODEX_HOME 之外的绝对路径不会出现在技能目录里
（`codex exec` 的请求体里看不到该技能）；而 `skills/` 目录被当作技能根，
链接进去的技能会进「Available skills」。链接失败（非 NTFS、无权限）时退回
复制——仍然只读，且只落在应用自己的目录里。
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
from pathlib import Path

from ... import config

log = logging.getLogger("wrk.agent.codex_home")

HOME_DIR_NAME = "agent_codex_home"
CONFIG_NAME = "config.toml"
SKILLS_DIR_NAME = "skills"
SKILLS_INDEX_NAME = ".wrk-app-skills.json"
SKILL_FILE = "SKILL.md"

CONFIG_TEXT = """\
# 这个目录由应用里「个人 Agent」的 Codex 核心独占（CODEX_HOME），请勿手工编辑。
#
# 它和用户自己的 ~/.codex 完全隔离：应用不读取用户的 AGENTS.md、config.toml、
# hooks、memories、人格设定、登录态与历史会话，也不会改写它们。
# 模型与供应商每轮由应用通过 `-c` 参数注入；用户的 Codex 技能以只读链接挂在
# 本目录的 skills/ 下，仅供复用。
"""


class CodexHomeError(RuntimeError):
    """隔离目录不可用。消息直接给用户看，所以要写清是哪一步失败。"""


# ------------------------------------------------------------------ 路径
def home_dir() -> Path:
    """应用自己的 Codex 配置目录（数据目录下的持久路径，所有会话共用）。"""
    return Path(config.DATA_DIR) / HOME_DIR_NAME


def user_codex_home() -> Path:
    """用户自己的 Codex 目录，只用来**读**技能。

    刻意不认 `CODEX_HOME` 环境变量：那可能是别的程序（甚至本应用自己）设的，
    指过去会错把别人的目录当成用户的技能来源。
    """
    return Path.home() / ".codex"


def user_skills_dir() -> Path:
    return user_codex_home() / "skills"


# ------------------------------------------------------------------ 准备
def ensure_dir() -> str:
    """建目录并保证有一份应用自己的 config.toml；返回路径字符串。

    版本探测这类「只看能不能跑」的调用只需要目录存在，不必同步技能，所以单独一层。
    """
    home = home_dir()
    try:
        home.mkdir(parents=True, exist_ok=True)
        config_path = home / CONFIG_NAME
        if not config_path.is_file() or config_path.read_text(encoding="utf-8") != CONFIG_TEXT:
            config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    except OSError as exc:
        raise CodexHomeError(
            f"无法准备 Codex 的隔离配置目录（{home}）：{exc}") from exc
    return str(home)


def prepare() -> dict:
    """启动一轮 Codex 之前的完整准备：目录 + config.toml + 技能链接。"""
    home = Path(ensure_dir())
    skills = sync_skills(home)
    return {"home": str(home), "config": str(home / CONFIG_NAME), "skills": skills}


def sync_skills(home: Path | None = None) -> dict:
    """把用户 `~/.codex/skills` 下已登记的技能只读挂进 `<home>/skills`。

    幂等：源技能没变时只做一次目录比对；源里删掉的技能会把我们挂过的那份撤掉，
    但只撤索引里记录过、且确实是我们创建的条目。
    """
    home = Path(home or home_dir())
    skills_root = home / SKILLS_DIR_NAME
    source_root = user_skills_dir()
    desired = _scan_source(source_root)
    index = _read_index(skills_root)
    stats = {"source": str(source_root), "linked": 0, "copied": 0,
             "removed": 0, "skipped": 0, "errors": []}

    try:
        skills_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CodexHomeError(f"无法创建技能目录（{skills_root}）：{exc}") from exc

    # 1) 源里已经没有的技能：撤掉我们挂过的那份
    for name in sorted(set(index) - set(desired)):
        path = skills_root / name
        if _is_managed(path, index.get(name)):
            try:
                _remove_path(path)
                stats["removed"] += 1
            except OSError as exc:      # 撤不掉就留着，下一轮再试
                log.warning("移除失效的 Agent 技能 %s 失败：%s", name, exc)
        index.pop(name, None)

    # 2) 新增或源已变化的技能
    for name, spec in sorted(desired.items()):
        path = skills_root / name
        previous = index.get(name)
        if previous and _same_source(previous, spec) and _is_managed(path, previous):
            continue
        try:
            _remove_if_managed(path, previous)
            kind = _expose(Path(spec["source"]), path)
            index[name] = {**spec, "kind": kind}
            stats["linked" if kind != "copy" else "copied"] += 1
        except OSError as exc:
            index.pop(name, None)
            stats["errors"].append({"skill": name, "error": str(exc)})
            log.warning("暴露 Codex 技能 %s 失败：%s", name, exc)

    stats["skipped"] = len(set(desired) - set(index))
    _write_index(skills_root, index)
    return stats


def _scan_source(source_root: Path) -> dict[str, dict]:
    """扫用户技能目录：只认「目录里有 SKILL.md」的条目，跳过 `.system` 等隐藏目录。"""
    found: dict[str, dict] = {}
    if not source_root.is_dir():
        return found
    try:
        entries = sorted(source_root.iterdir())
    except OSError as exc:
        log.warning("读取用户 Codex 技能目录失败（%s）：%s", source_root, exc)
        return found
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        skill_md = entry / SKILL_FILE
        try:
            if not skill_md.is_file():
                continue
            stat = skill_md.stat()
        except OSError:                 # 单个技能坏了不影响其它技能
            continue
        found[entry.name] = {
            "source": str(entry),
            "mtime": int(stat.st_mtime),
            "size": int(stat.st_size),
        }
    return found


def _same_source(previous: dict, spec: dict) -> bool:
    return (previous.get("source") == spec["source"]
            and previous.get("mtime") == spec["mtime"]
            and previous.get("size") == spec["size"])


# ------------------------------------------------------------------ 索引
def _read_index(skills_root: Path) -> dict[str, dict]:
    path = skills_root / SKILLS_INDEX_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    entries = data.get("skills")
    if not isinstance(entries, dict):
        return {}
    out: dict[str, dict] = {}
    for name, value in entries.items():
        if isinstance(value, dict) and isinstance(value.get("source"), str):
            out[str(name)] = value
    return out


def _write_index(skills_root: Path, index: dict[str, dict]) -> None:
    path = skills_root / SKILLS_INDEX_NAME
    payload = json.dumps({"skills": index}, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        path.write_text(payload, encoding="utf-8")
    except OSError as exc:
        log.warning("写入 Codex 技能索引失败（%s）：%s", path, exc)


# ------------------------------------------------------------------ 链接
def _link_target(path: Path) -> str | None:
    """取符号链接/目录联接指向的目标；不是链接返回 None。

    Windows 的目录联接（junction）不算 `os.path.islink`，但 `os.readlink` 能读出来，
    所以统一用 readlink 判断。
    """
    try:
        raw = os.readlink(path)
    except OSError:
        return None
    except ValueError:                  # 某些平台对非链接会给 ValueError
        return None
    return raw.replace("\\\\?\\", "")


def _is_managed(path: Path, spec: dict | None) -> bool:
    """这个条目是不是我们创建的：链接的看目标，复制的看索引。"""
    target = _link_target(path)
    if target is not None:
        source = (spec or {}).get("source")
        if source:
            return _same_path(target, source)
        return True                     # 没索引也认：目标落在我们目录里的链接只可能是我们建的
    return bool(spec and spec.get("kind") == "copy")


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _remove_if_managed(path: Path, spec: dict | None) -> None:
    if os.path.lexists(path) and _is_managed(path, spec):
        _remove_path(path)


def _remove_path(path: Path) -> None:
    """只删链接本身或我们自己的副本；绝不动链接目标。"""
    if os.path.lexists(path) and _link_target(path) is not None:
        try:
            os.rmdir(path)              # Windows 目录联接
        except OSError:
            os.unlink(path)             # POSIX 符号链接
        return
    if path.is_dir():
        shutil.rmtree(path)
    elif os.path.lexists(path):
        path.unlink()


def _expose(source: Path, path: Path) -> str:
    """在 path 上暴露 source：优先目录联接/符号链接，都不行才复制。"""
    if os.path.lexists(path):
        raise OSError(f"目标已存在：{path}")
    if os.name == "nt":
        try:
            _make_junction(path, source)
            return "junction"
        except OSError as exc:
            log.debug("创建目录联接失败（%s），改试符号链接：%s", path, exc)
    try:
        os.symlink(source, path, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        pass
    shutil.copytree(source, path)
    return "copy"


# Windows 目录联接（junction）：不需要管理员权限，也不需要开发者模式，
# 而符号链接两者都要，所以在 Windows 上优先用它。
_FSCTL_SET_REPARSE_POINT = 0x000900A4
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
# REPARSE_DATA_BUFFER：ReparseTag(4) + ReparseDataLength(2) + Reserved(2) +
# SubstituteNameOffset/Length、PrintNameOffset/Length（各 2）= 16 字节，之后才是路径。
_REPARSE_BUFFER_HEADER_BYTES = 16
# ReparseDataLength 从「4 个偏移/长度字段」开始算，同样不含上面那 8 字节头。
_MOUNT_POINT_HEADER_BYTES = 8


class _ReparseDataBuffer(ctypes.Structure):
    _fields_ = [
        ("ReparseTag", ctypes.c_ulong),
        ("ReparseDataLength", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort),
        ("SubstituteNameOffset", ctypes.c_ushort),
        ("SubstituteNameLength", ctypes.c_ushort),
        ("PrintNameOffset", ctypes.c_ushort),
        ("PrintNameLength", ctypes.c_ushort),
        ("PathBuffer", ctypes.c_wchar * 1),
    ]


def _make_junction(link_path: Path, target: Path) -> None:
    """把 link_path（必须是已存在的空目录）变成指向 target 的目录联接。"""
    substitute = "\\??\\" + os.path.abspath(str(target))
    substitute_bytes = substitute.encode("utf-16-le")
    print_bytes = os.path.abspath(str(target)).encode("utf-16-le")
    payload = substitute_bytes + b"\x00\x00" + print_bytes + b"\x00\x00"

    buffer = ctypes.create_string_buffer(_REPARSE_BUFFER_HEADER_BYTES + len(payload))
    header = _ReparseDataBuffer.from_buffer(buffer)
    header.ReparseTag = _IO_REPARSE_TAG_MOUNT_POINT
    header.ReparseDataLength = _MOUNT_POINT_HEADER_BYTES + len(payload)
    header.SubstituteNameLength = len(substitute_bytes)
    header.PrintNameOffset = len(substitute_bytes) + 2
    header.PrintNameLength = len(print_bytes)
    ctypes.memmove(
        ctypes.addressof(buffer) + _ReparseDataBuffer.PathBuffer.offset,
        payload, len(payload),
    )

    link_path.mkdir(parents=True, exist_ok=False)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(
        str(link_path), _GENERIC_WRITE, _FILE_SHARE_ALL, None, _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT, None,
    )
    if not handle or handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        _remove_path(link_path)
        raise OSError(error, f"打开目录失败：{link_path}")
    try:
        returned = ctypes.c_ulong(0)
        ok = kernel32.DeviceIoControl(
            ctypes.c_void_p(handle), _FSCTL_SET_REPARSE_POINT, buffer,
            _REPARSE_BUFFER_HEADER_BYTES + len(payload), None, 0,
            ctypes.byref(returned), None,
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), f"设置目录联接失败：{link_path}")
    except OSError:
        _remove_path(link_path)
        raise
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
