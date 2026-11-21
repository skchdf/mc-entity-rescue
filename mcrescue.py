#!/usr/bin/env python3
"""
mcrescue — Minecraft Java 版存档实体抢救工具（零依赖，纯标准库）

场景：备份与主存档分叉后，主存档丢失了部分实体（村民被误杀、区块被误改等），
想把备份里的实体补回主存档，但**不回滚**你后来的建筑与地形。

原理：实体在区块 NBT 中与方块数据是并列字段。本工具只做"实体级合并"——
解析两边区块 NBT，把备份中主存档已不存在的实体（按 UUID 全存档查重）
追加进主存档区块的实体列表，方块数据原样保留。

子命令：
  info    查看存档基本信息（名称、版本、实体统计）
  where   方块坐标 -> 区块坐标 / 区域文件 / 表头索引
  scan    对比两个存档，列出"备份有、主存档没有"的实体及所在区块
  recover 把丢失实体合并回主存档（默认 dry-run，加 --apply 才写盘）
  verify  校验：备份实体是否齐全、主存档有无重复 UUID

用法示例：
  python3 mcrescue.py info   /path/to/World
  python3 mcrescue.py scan   /path/to/World /path/to/World-copy
  python3 mcrescue.py recover /path/to/World /path/to/World-copy --apply
  python3 mcrescue.py verify /path/to/World /path/to/World-copy

注意：运行前请完全关闭 Minecraft。

兼容性：实测于 Java 1.16.5（Anvil + 区块内 Entities）。解析器支持全部
标准 NBT 标签、gzip/zlib/未压缩三种区块压缩，以及超大区块的外部
c.x.z.mcc 存储；字符串按 Java 的 modified UTF-8 处理（带 emoji 的
自定义名字也能字节级往返保真）。区块布局自动识别：<=1.16 的
Level.Entities、1.17（20w45a）起独立的 entities 目录、21w43a+ 根级
小写 entities（生成中的区块）、26.1+ 的 dimensions/minecraft/* 布局，
以及一直存在的数据包/模组自定义维度目录。
"""

import argparse
import gzip
import os
import shutil
import struct
import sys
import time
import zlib

PROG = "mcrescue"

# 永远不需要恢复的实体：掉落物 5 分钟自然消失，且可能已被玩家捡走
DEFAULT_SKIP_TYPES = {"minecraft:item"}

# 可预期的"数据坏了"异常：救援工具的正确反应是跳过并计数，而不是崩掉整次扫描。
# 不在此列的异常类型属于代码 bug，--debug 时会原样抛出（见 chunk_parse /
# index_entities）。
PARSE_ERRORS = (struct.error, ValueError, IndexError, EOFError,
                zlib.error, OSError, UnicodeDecodeError)

# 少量常见 DataVersion 对照（仅用于 info 显示，未知版本显示原值）
DATA_VERSIONS = {
    2566: "1.16", 2586: "1.16.5", 2717: "1.17", 2730: "1.17.1",
    2860: "1.18", 2975: "1.18.2", 3105: "1.19", 3337: "1.19.4",
    3465: "1.20.1", 3578: "1.20.2", 3698: "1.20.3", 3700: "1.20.4",
    3837: "1.20.5", 3839: "1.20.6", 3953: "1.21", 3955: "1.21.1",
    4080: "1.21.2", 4082: "1.21.3", 4189: "1.21.4", 4325: "1.21.5",
}

# ---------------------------------------------------------------------------
# NBT：带类型的解析与序列化（往返保真）
# ---------------------------------------------------------------------------

class Node:
    """一个 NBT 标签：t = 标签类型(1-12)，v = 值。"""
    __slots__ = ("t", "v")

    def __init__(self, t, v):
        self.t = t
        self.v = v


def _decode_str(b):
    """NBT 字符串用 Java 的 modified UTF-8：非 BMP 字符（emoji 等）按
    UTF-16 代理对编码（各 3 字节）。先按 surrogatepass 解（无损往返），
    失败再退化成替换字符——坏数据不炸整块。"""
    try:
        return b.decode("utf-8", "surrogatepass")
    except UnicodeDecodeError:
        return b.decode("utf-8", "replace")


def _encode_str(s):
    """与 _decode_str 配对：代理对原样写回，保证字节级往返。"""
    return s.encode("utf-8", "surrogatepass")


def _read_node(t, d, p):
    if t == 1:   return Node(t, struct.unpack_from(">b", d, p)[0]), p + 1
    if t == 2:   return Node(t, struct.unpack_from(">h", d, p)[0]), p + 2
    if t == 3:   return Node(t, struct.unpack_from(">i", d, p)[0]), p + 4
    if t == 4:   return Node(t, struct.unpack_from(">q", d, p)[0]), p + 8
    if t == 5:   return Node(t, struct.unpack_from(">f", d, p)[0]), p + 4
    if t == 6:   return Node(t, struct.unpack_from(">d", d, p)[0]), p + 8
    if t == 7:
        n = struct.unpack_from(">i", d, p)[0]; p += 4
        if n < 0 or p + n > len(d):
            raise ValueError(f"TAG_Byte_Array 长度非法: {n}")
        return Node(t, d[p:p + n]), p + n
    if t == 8:
        n = struct.unpack_from(">H", d, p)[0]; p += 2
        if p + n > len(d):
            raise ValueError(f"TAG_String 长度越界: {n}")
        return Node(t, _decode_str(d[p:p + n])), p + n
    if t == 9:
        et = d[p]
        n = struct.unpack_from(">i", d, p + 1)[0]
        p += 5
        if n < 0:
            raise ValueError(f"TAG_List 元素数为负: {n}")
        items = []
        for _ in range(n):
            nd, p = _read_node(et, d, p)
            items.append(nd)
        return Node(t, (et, items)), p
    if t == 10:
        entries = []
        while True:
            if p >= len(d):
                raise ValueError("TAG_Compound 缺少 0x00 结束符")
            tt = d[p]; p += 1
            if tt == 0:
                break
            if p + 2 > len(d):
                raise ValueError("TAG_Compound 条目名称长度越界")
            ln = struct.unpack_from(">H", d, p)[0]; p += 2
            if p + ln > len(d):
                raise ValueError("TAG_Compound 条目名称越界")
            name = _decode_str(d[p:p + ln]); p += ln
            nd, p = _read_node(tt, d, p)
            entries.append((name, nd))
        return Node(t, entries), p
    if t == 11:
        n = struct.unpack_from(">i", d, p)[0]; p += 4
        if n < 0 or p + 4 * n > len(d):
            raise ValueError(f"TAG_Int_Array 长度非法: {n}")
        return Node(t, list(struct.unpack_from(f">{n}i", d, p))), p + 4 * n
    if t == 12:
        n = struct.unpack_from(">i", d, p)[0]; p += 4
        if n < 0 or p + 8 * n > len(d):
            raise ValueError(f"TAG_Long_Array 长度非法: {n}")
        return Node(t, list(struct.unpack_from(f">{n}q", d, p))), p + 8 * n
    raise ValueError(f"未知 NBT 标签类型 {t} @ {p}")

def parse_root(d):
    """解析一条完整的 NBT（命名根标签）。返回 (name, Node)。"""
    if len(d) < 3:
        raise ValueError("NBT 数据过短")
    t = d[0]
    ln = struct.unpack_from(">H", d, 1)[0]
    if 3 + ln > len(d):
        raise ValueError("根标签名称越界")
    name = _decode_str(d[3:3 + ln])
    node, _ = _read_node(t, d, 3 + ln)
    return name, node


def _write_node(nd, out):
    t, v = nd.t, nd.v
    if t == 1:    out.append(struct.pack(">b", v))
    elif t == 2:  out.append(struct.pack(">h", v))
    elif t == 3:  out.append(struct.pack(">i", v))
    elif t == 4:  out.append(struct.pack(">q", v))
    elif t == 5:  out.append(struct.pack(">f", v))
    elif t == 6:  out.append(struct.pack(">d", v))
    elif t == 7:
        out.append(struct.pack(">i", len(v))); out.append(v)
    elif t == 8:
        b = _encode_str(v)
        out.append(struct.pack(">H", len(b))); out.append(b)
    elif t == 9:
        et, items = v
        if items:
            et = items[0].t
        out.append(struct.pack(">b", et))
        out.append(struct.pack(">i", len(items)))
        for it in items:
            _write_node(it, out)
    elif t == 10:
        for name, nd2 in v:
            b = _encode_str(name)
            out.append(struct.pack(">B", nd2.t))
            out.append(struct.pack(">H", len(b)))
            out.append(b)
            _write_node(nd2, out)
        out.append(b"\x00")
    elif t == 11:
        out.append(struct.pack(">i", len(v)))
        out.append(struct.pack(f">{len(v)}i", *v))
    elif t == 12:
        out.append(struct.pack(">i", len(v)))
        out.append(struct.pack(f">{len(v)}q", *v))
    else:
        raise ValueError(f"未知 NBT 标签类型 {t}")

def write_root(name, node):
    out = [struct.pack(">B", node.t)]
    nb = _encode_str(name)
    out.append(struct.pack(">H", len(nb)))
    out.append(nb)
    _write_node(node, out)
    return b"".join(out)


def tag_get(comp, key):
    """在 compound 中按键取子标签，不存在返回 None。"""
    if comp is None or comp.t != 10:
        return None
    for n, nd in comp.v:
        if n == key:
            return nd
    return None


def entity_uuid(ent):
    """返回实体 UUID 的 4 段无符号 32 位元组，没有则 None。

    兼容两种存法并归一化成同一表示——这是全存档查重防复制的根基，
    两种存法必须能互相匹配，否则同一只实体会被复制出第二份：
      1.16+   UUID: TAG_Int_Array[4]，从高位到低位
      旧版    UUIDMost / UUIDLeast：两个 TAG_Long，UUID = Most<<64 | Least
    """
    u = tag_get(ent, "UUID")
    if u is not None and u.t == 11 and len(u.v) == 4:
        return tuple(x & 0xFFFFFFFF for x in u.v)
    most, least = tag_get(ent, "UUIDMost"), tag_get(ent, "UUIDLeast")
    if most is not None and least is not None:
        m = most.v & 0xFFFFFFFFFFFFFFFF
        l = least.v & 0xFFFFFFFFFFFFFFFF
        return (m >> 32, m & 0xFFFFFFFF, l >> 32, l & 0xFFFFFFFF)
    return None


def entity_type(ent):
    nd = tag_get(ent, "id")
    return nd.v if nd is not None else "?"


def entity_profession(ent):
    vd = tag_get(ent, "VillagerData")
    p = tag_get(vd, "profession") if vd is not None else None
    return p.v if p is not None else ""


def get_entities_list(root):
    """取区块实体列表 Node（List of Compound）。
    兼容布局：
      <=1.16      Level.Entities
      实体文件     根级 Entities（20w45a 起，键名大写）
      21w43a+     根级 entities（地形区块键名小写化，仅生成中的区块有内容）
    """
    if root is None or root.t != 10:
        return None, None
    level = tag_get(root, "Level")
    if level is not None:
        ents = tag_get(level, "Entities")
        if ents is not None and ents.t == 9:
            return level, ents
    for n, nd in root.v:
        if n.lower() == "entities" and nd.t == 9:
            return root, nd
    return None, None


# ---------------------------------------------------------------------------
# Anvil 区域文件（r.X.Z.mca）读写
# ---------------------------------------------------------------------------

def decompress(comp, payload):
    """按区块头的压缩类型字节解压：1=gzip, 2=zlib, 3=原样。"""
    if comp == 2:
        return zlib.decompress(payload)
    if comp == 1:
        return gzip.decompress(payload)
    return payload


def load_region(path):
    """读取区域文件 -> ({局部索引: [压缩类型, 数据]}, {索引: 时间戳})

    超过 255 扇区（约 1 MiB）的区块按 Anvil 规范存放在同目录的外部文件
    c.<局部x>.<局部z>.mcc 里（槽位压缩字节 |= 0x80、length=1），自动读取。
    截断 / 损坏的部分能跳则跳，不让整个文件读不出来。
    """
    chunks, stamps = {}, {}
    ddir = os.path.dirname(path)
    with open(path, "rb") as f:
        header = f.read(8192)
        if len(header) < 8192:
            return chunks, stamps  # 0 字节/损坏的空文件，跳过
        for i in range(1024):
            entry = struct.unpack(">I", header[i * 4:i * 4 + 4])[0]
            off = (entry >> 8) * 4096
            if off == 0:
                continue
            ts = struct.unpack(">I", header[4096 + i * 4:4100 + i * 4])[0]
            f.seek(off)
            head = f.read(5)
            if len(head) < 5:
                break  # 截断文件
            length = struct.unpack(">I", head[:4])[0]
            comp = head[4]
            if comp & 0x80:
                # 数据在外部 c.x.z.mcc（只含压缩数据本体，无长度头）
                comp &= 0x7F
                mcc = os.path.join(ddir, f"c.{i % 32}.{i // 32}.mcc")
                try:
                    with open(mcc, "rb") as mf:
                        payload = mf.read()
                except OSError:
                    continue  # 外部文件丢了，跳过该区块
                if not payload:
                    continue
            else:
                if length < 1:
                    break  # 损坏的长度字段
                payload = f.read(length - 1)
                if len(payload) != length - 1:
                    break  # 截断文件
            chunks[i] = [comp, payload]
            stamps[i] = ts
    return chunks, stamps


def save_region(path, chunks, stamps):
    """重写区域文件：8KB 头表 + 顺序追加扇区。原子替换（先写 .tmp 再 rename）。

    单区块压缩后超过 255 扇区时按 Anvil 规范降级：数据写入同目录的
    c.<局部x>.<局部z>.mcc，区域槽位只放 length=1、压缩字节 |= 0x80 的占位。
    """
    header = bytearray(8192)
    body = bytearray()
    ddir = os.path.dirname(path)
    next_off = 2  # 扇区号，数据从 8192 字节（扇区 2）开始
    for i in range(1024):
        if i not in chunks:
            continue
        comp, payload = chunks[i]
        data = struct.pack(">I", len(payload) + 1) + bytes([comp]) + payload
        sectors = (len(data) + 4095) // 4096
        if sectors > 255:
            mcc = os.path.join(ddir, f"c.{i % 32}.{i // 32}.mcc")
            mcc_tmp = mcc + ".tmp"
            with open(mcc_tmp, "wb") as mf:
                mf.write(payload)
                mf.flush()
                os.fsync(mf.fileno())
            os.replace(mcc_tmp, mcc)
            data = struct.pack(">I", 1) + bytes([comp | 0x80])
            sectors = 1
        struct.pack_into(">I", header, i * 4, (next_off << 8) | sectors)
        struct.pack_into(">I", header, 4096 + i * 4, stamps.get(i, 0))
        body += data
        body += b"\x00" * (sectors * 4096 - len(data))
        next_off += sectors
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(bytes(header))
        f.write(bytes(body))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def is_entity_subdir(s):
    """子目录是否为 1.17+（20w45a）独立实体目录（entities）。"""
    return s.split("/")[-1] == "entities"


def _dimension_storage_dirs(save_root):
    """dimensions/<命名空间>/<维度>/... 下所有含 region 或 entities 子目录的
    目录（相对路径，/ 分隔）。原版 26.1+ 的三个维度和一直存在的数据包/
    模组自定义维度都在这棵树下，深度不固定，直接 walk。"""
    base = os.path.join(save_root, "dimensions")
    found = []
    if os.path.isdir(base):
        for dirpath, dirnames, _ in os.walk(base):
            rel = os.path.relpath(dirpath, save_root).replace(os.sep, "/")
            for kind in ("region", "entities"):
                if kind in dirnames:
                    found.append(rel + "/" + kind)
    return found


def region_dirs(save_root):
    """存档里所有可能存放区域文件的子目录（region 与 entities 两种）。
    覆盖三代布局：
      <=26.1 前    region、DIM-1/region、DIM1/region，
                   以及 1.17+（20w45a）各自对应的 entities 目录
      26.1+        dimensions/minecraft/{overworld,the_nether,the_end}/...
      自定义维度    dimensions/<命名空间>/<维度>/...（深度不限）
    """
    dirs = [
        "region", "entities",
        "DIM-1/region", "DIM-1/entities",
        "DIM1/region", "DIM1/entities",
    ]
    dirs.extend(_dimension_storage_dirs(save_root))
    return list(dict.fromkeys(dirs))


def region_files(save_root, sub=None):
    """列出存档中所有区域文件，yield (子目录, 文件名, rx, rz, 完整路径)。"""
    subs = (sub,) if sub else region_dirs(save_root)
    for s in subs:
        rdir = os.path.join(save_root, s)
        if not os.path.isdir(rdir):
            continue
        for fn in sorted(os.listdir(rdir)):
            if not fn.endswith(".mca"):
                continue
            parts = fn.split(".")
            try:
                rx, rz = int(parts[1]), int(parts[2])
            except (IndexError, ValueError):
                continue
            yield s, fn, rx, rz, os.path.join(rdir, fn)


def chunk_parse(comp, payload, debug=False):
    """解压 + 解析一个区块，返回 (根名称, 根Node)；失败返回 (None, None)。"""
    try:
        name, root = parse_root(decompress(comp, payload))
        return name, root
    except PARSE_ERRORS:
        return None, None  # 数据坏：救援工具宁可跳过
    except Exception:
        if debug:
            raise
        return None, None


_ENT_TAG = b"\x09\x00\x08Entities"    # List 类型 + 名称长度 8 + "Entities"
_ENT_TAG_LC = b"\x09\x00\x08entities"  # 21w43a+ 地形区块的小写键名


def _tag_list_all_empty(raw, tag):
    """名为 tag 的 List 标签：键名没出现、或所有出现位置计数都为 0 -> True；
    存在任何一处非空（或数据不足以判断）-> False。"""
    start = 0
    while True:
        i = raw.find(tag, start)
        if i < 0:
            return True
        p = i + len(tag)
        if p + 5 > len(raw) or int.from_bytes(raw[p + 1:p + 5], "big") != 0:
            return False  # 非空实体列表（或截断看不完）-> 必须完整解析
        start = i + 1


def entities_empty(raw):
    """性能优化：仅当确认 NBT 中不存在任何非空实体列表时返回 True
    （可跳过整块解析）。键名完全没出现也算空；任何不确定的情况都返回
    False 以保证正确。"""
    return (_tag_list_all_empty(raw, _ENT_TAG) and
            _tag_list_all_empty(raw, _ENT_TAG_LC))


# ---------------------------------------------------------------------------
# 存档级扫描
# ---------------------------------------------------------------------------

def index_entities(save_root, progress=None, verbose=False, debug=False):
    """扫描整个存档的实体。
    返回：
      uuid_map:  {(实体类型, UUID): 首次出现位置 (子目录, 文件名, gx, gz)}
      chunk_map: {(子目录, 文件名, gx, gz): [(实体类型, UUID, 职业), ...]}
      dup_map:   {(实体类型, UUID): [出现位置, ...]}，仅收录出现 >= 2 次的

    verbose=True 时把各类"跳过"的区块数打印出来（含启发式捷径跳过的数量），
    便于核对有没有因捷径漏掉本该恢复的区块；debug=True 时非预期异常
    （疑似代码 bug）原样抛出而不是当作坏数据跳过。
    """
    uuid_map, chunk_map, dup_map = {}, {}, {}
    skipped = {"region": 0, "bad_chunk": 0, "empty": 0, "heuristic": 0, "parse": 0}
    for s, fn, rx, rz, path in region_files(save_root):
        if progress:
            progress(f"{s}/{fn}")
        try:
            chunks, _ = load_region(path)
        except OSError:
            skipped["region"] += 1
            continue  # 读不了的文件直接跳过
        for idx, (comp, payload) in chunks.items():
            try:
                raw = decompress(comp, payload)
            except PARSE_ERRORS:
                skipped["bad_chunk"] += 1
                continue  # 坏区块跳过，不影响其它区块
            except Exception:
                if debug:
                    raise
                skipped["bad_chunk"] += 1
                continue
            if b"Entities" not in raw and b"entities" not in raw:
                skipped["empty"] += 1
                continue  # 键名完全不存在：确定无实体
            if entities_empty(raw):
                # 字节模式启发式捷径：任何非零计数都会退回完整解析，
                # 但理论上可能被恰好同形的字节序列骗过——计数并可在
                # verbose 下核对
                skipped["heuristic"] += 1
                continue
            try:
                _, root = parse_root(raw)
            except PARSE_ERRORS:
                skipped["parse"] += 1
                continue
            except Exception:
                if debug:
                    raise
                skipped["parse"] += 1
                continue
            _, ents = get_entities_list(root)
            if ents is None:
                continue
            gx, gz = rx * 32 + idx % 32, rz * 32 + idx // 32
            key = (s, fn, gx, gz)
            for ent in ents.v[1]:
                et = entity_type(ent)
                u = entity_uuid(ent)
                rec = (et, u, entity_profession(ent))
                chunk_map.setdefault(key, []).append(rec)
                if u is not None:
                    k = (et, u)
                    if k in uuid_map:
                        dup_map.setdefault(k, [uuid_map[k]]).append(key)
                    else:
                        uuid_map[k] = key
    if verbose and any(skipped.values()):
        print("\n扫描跳过统计（这些区块未纳入实体索引）:")
        print(f"  区域文件不可读        : {skipped['region']}")
        print(f"  坏区块（解压/解析失败）: {skipped['bad_chunk']}")
        print(f"  无实体键名            : {skipped['empty']}")
        print(f"  启发式判定为空        : {skipped['heuristic']}")
        print(f"  完整解析失败          : {skipped['parse']}")
    return uuid_map, chunk_map, dup_map


def parse_chunk_coord(x, z):
    """方块坐标 -> 区块/区域坐标。"""
    cx, cz = x >> 4, z >> 4
    rx, rz = cx >> 5, cz >> 5
    idx = (cx & 31) + (cz & 31) * 32
    return cx, cz, rx, rz, idx


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------

def cmd_where(args):
    try:
        coords = [int(v) for v in args.block.split(",")]
    except ValueError:
        print(f"错误: 无法解析坐标 {args.block!r}（示例: \"63,58,45\"）",
              file=sys.stderr)
        return 2
    if len(coords) < 2:
        print("错误: 至少需要 x,z 两个数字（x,y,z 三个也行）", file=sys.stderr)
        return 2
    x = coords[0]
    z = coords[2] if len(coords) >= 3 else coords[1]
    cx, cz, rx, rz, idx = parse_chunk_coord(x, z)
    print(f"方块坐标   : {args.block}")
    print(f"区块坐标   : ({cx}, {cz})   [方块范围 x {cx * 16}..{cx * 16 + 15}, z {cz * 16}..{cz * 16 + 15}]")
    print(f"区域文件   : r.{rx}.{rz}.mca")
    print(f"表头索引   : {idx}   (局部({cx & 31}, {cz & 31}))")
    if args.save:
        path = os.path.join(args.save, "region", f"r.{rx}.{rz}.mca")
        if not os.path.exists(path):
            print("区块状态   : 尚未生成")
            return 0
        chunks, _ = load_region(path)
        print("区块状态   : %s" % ("存在" if idx in chunks else "尚未生成"))
    return 0


def cmd_info(args):
    save = args.save
    ld = os.path.join(save, "level.dat")
    if not os.path.exists(ld):
        print(f"错误: 找不到 {ld}（这不像一个存档目录）", file=sys.stderr)
        return 2
    try:
        with gzip.open(ld, "rb") as f:
            _, root = parse_root(f.read())
    except Exception as e:  # noqa: BLE001 — level.dat 任何形式的损坏都归为"读取失败"，不能崩
        print(f"错误: level.dat 读取失败: {e}", file=sys.stderr)
        return 2
    data = tag_get(root, "Data") or root
    name = tag_get(data, "LevelName")
    dv = tag_get(data, "DataVersion")
    ver = tag_get(data, "Version")
    spawn = (tag_get(data, "SpawnX"), tag_get(data, "SpawnY"), tag_get(data, "SpawnZ"))
    print(f"存档       : {save}")
    if name is not None:
        print(f"名称       : {name.v}")
    if dv is not None:
        mcver = DATA_VERSIONS.get(dv.v)
        print(f"DataVersion: {dv.v}{f'  ({mcver})' if mcver else ''}")
    if ver is not None:
        vn = tag_get(ver, "Name")
        if vn is not None:
            print(f"版本       : {vn.v}")
    if all(s is not None for s in spawn):
        print(f"出生点     : ({spawn[0].v}, {spawn[1].v}, {spawn[2].v})")

    print("\n扫描实体 ...")
    uuid_map, chunk_map, dup_map = index_entities(save, verbose=args.verbose, debug=args.debug)
    by_type = {}
    for (et, u) in uuid_map:
        by_type[et] = by_type.get(et, 0) + 1
    no_uuid = sum(1 for recs in chunk_map.values() for r in recs if r[1] is None)
    total = sum(len(v) for v in chunk_map.values())
    print(f"含实体区块 : {len(chunk_map)}")
    print(f"实体总数   : {total}（其中无 UUID {no_uuid}）")
    if dup_map:
        print(f"重复 UUID  : {len(dup_map)}（运行 verify 看详情）")
    for et, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
        print(f"  {et:<32} {n}")
    return 0


def cmd_scan(args):
    print(f"索引备份存档: {args.backup}")
    _, b_chunks, _ = index_entities(args.backup, verbose=args.verbose, debug=args.debug)
    print(f"索引主存档  : {args.main}")
    m_uuid, _, _ = index_entities(args.main, verbose=args.verbose, debug=args.debug)

    wanted = set(args.types.split(",")) if args.types else None
    skip = set() if args.include_items else DEFAULT_SKIP_TYPES

    missing_by_chunk = {}
    missing_total = 0
    for key, recs in b_chunks.items():
        s, fn, gx, gz = key
        for et, u, prof in recs:
            if et in skip:
                continue
            if wanted is not None and et not in wanted:
                continue
            if u is not None and (et, u) in m_uuid:
                continue
            missing_by_chunk.setdefault(key, []).append((et, u, prof))
            missing_total += 1

    if not missing_by_chunk:
        print("\n没有发现缺失实体（按当前过滤条件）。")
        return 0

    print(f"\n发现 {missing_total} 只实体缺失，分布在 {len(missing_by_chunk)} 个区块：")
    for key in sorted(missing_by_chunk, key=lambda k: (k[0], k[1], k[2], k[3])):
        s, fn, gx, gz = key
        recs = missing_by_chunk[key]
        print(f"\n  {s}/{fn}  全局区块({gx}, {gz})  方块 x {gx * 16}..{gx * 16 + 15}, z {gz * 16}..{gz * 16 + 15}")
        summary = {}
        for et, u, prof in recs:
            label = et if not prof else "{}({})".format(et.split(":")[-1], prof.split(":")[-1])
            summary[label] = summary.get(label, 0) + 1
        for label, n in sorted(summary.items(), key=lambda kv: -kv[1]):
            print(f"      {label:<40} x{n}")
    print(f"\n下一步: python3 {PROG} recover {args.main} {args.backup}   # 默认只预览，加 --apply 才写盘")
    return 0


def plan_recovery(main_root, backup_root, types_wanted, skip_items, verbose=False, debug=False):
    """计算需要从备份恢复到主存档的实体。
    返回 (targets, stats, main_uuid_map):
      targets: {(子目录, 文件名, gx, gz): [(实体类型, UUID), ...]}
      stats:   各类计数
      main_uuid_map: 主存档的 {(类型, UUID): 位置}，执行阶段沿用，免二次全扫
    """
    print(f"索引备份存档: {backup_root}")
    _, b_chunks, _ = index_entities(backup_root, verbose=verbose, debug=debug)
    print(f"索引主存档  : {main_root}")
    m_uuid, _, _ = index_entities(main_root, verbose=verbose, debug=debug)

    skip = set() if not skip_items else DEFAULT_SKIP_TYPES
    wanted = types_wanted or None
    targets, stats = {}, {"missing": 0, "exists": 0, "no_uuid": 0}
    for key, recs in b_chunks.items():
        for et, u, prof in recs:
            if et in skip:
                continue
            if wanted is not None and et not in wanted:
                continue
            if u is None:
                stats["no_uuid"] += 1
                continue
            if (et, u) in m_uuid:
                stats["exists"] += 1
                continue
            targets.setdefault(key, []).append((et, u))
            stats["missing"] += 1
    return targets, stats, m_uuid


def _parse_chunks_filter(spec):
    """解析 --chunks "3,2;3,3" -> {(gx, gz), ...}；非法时抛 ValueError。"""
    want = set()
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        a, b = part.split(",")
        want.add((int(a), int(b)))
    return want


def _group_by_region(targets):
    """targets -> {("entities", "r.0.0.mca"): [(gx, gz, [(类型, UUID), ...]), ...]}"""
    by_region = {}
    for (s, fn, gx, gz), recs in targets.items():
        by_region.setdefault((s, fn), []).append((gx, gz, recs))
    return by_region


def _print_recovery_plan(targets, stats):
    """打印恢复计划摘要（含按区域文件的分布）。"""
    print("\n恢复计划:")
    print(f"  主存档已存在（跳过）: {stats['exists']}")
    print(f"  无 UUID（跳过）     : {stats['no_uuid']}")
    print(f"  待恢复              : {stats['missing']}")
    if not targets:
        print("\n无需任何修改。")
        return
    by_region = _group_by_region(targets)
    for (s, fn), items in sorted(by_region.items()):
        print(f"  {s}/{fn}: " + ", ".join(
            f"({gx},{gz})x{len(r)}" for gx, gz, r in items))


def execute_recovery(main_root, backup_root, targets, m_uuid, backup_dir,
                     verbose=False, debug=False):
    """执行恢复计划：把 targets 里的实体从备份合并进主存档并写盘。

    与规划（plan_recovery）分离：本函数只做 IO 与合并，错误分支
    （备份缺文件、主存档缺地形文件、区块缺半边、解析失败）都在这里，
    便于单独测试。verbose/debug 透传给区块解析。
    """
    all_uuids = set()  # 跨区块查重：本次运行内已恢复的 (类型, UUID)
    for (s, fn), items in sorted(_group_by_region(targets).items()):
        main_path = os.path.join(main_root, s, fn)
        backup_path = os.path.join(backup_root, s, fn)
        if not os.path.exists(backup_path):
            print(f"  !! 备份缺少 {s}/{fn}，跳过")
            continue
        if os.path.exists(main_path):
            # 自动备份原文件（保留子目录结构）
            bak = os.path.join(backup_dir, s, fn)
            os.makedirs(os.path.dirname(bak), exist_ok=True)
            if not os.path.exists(bak):
                shutil.copy2(main_path, bak)
                print(f"已备份 -> {bak}")
            m_chunks, m_stamps = load_region(main_path)
        elif is_entity_subdir(s):
            # 1.17+ 实体文件独立于地形：主存档还没有该实体文件时可以新建，
            # 只新建 entities/ 下的文件，不碰任何地形区块
            m_chunks, m_stamps = {}, {}
            print(f"  主存档尚无 {s}/{fn}，将新建实体文件")
        else:
            # 地形 region 文件整体缺失 = 主存档根本没有这些区块。
            # 凭空创建会把备份时的地形一并写进去（等于整块回滚），拒绝。
            print(f"  !! 主存档尚无 {s}/{fn}（地形文件缺失，为避免整块回滚不新建），跳过")
            continue
        c_chunks, _ = load_region(backup_path)
        need = {}
        for gx, gz, recs in items:
            idx = ((gx & 31) + (gz & 31) * 32)
            need[idx] = set(recs)  # {(实体类型, UUID), ...}

        if not m_chunks:
            # 新建实体文件：以备份区块为底稿（保留 DataVersion / Position 等），
            # 但清空实体列表内容——需要恢复的实体由下面的合并逻辑按
            # --types 过滤与查重后逐个追加，不会把未过滤的实体整包搬进来
            now = int(time.time())
            for idx in need:
                if idx in c_chunks:
                    name, croot = chunk_parse(*c_chunks[idx], debug=debug)
                    if croot is None:
                        continue
                    _, cents = get_entities_list(croot)
                    if cents is not None:
                        cents.v[1].clear()
                    m_chunks[idx] = [2, zlib.compress(write_root(name, croot))]
                    m_stamps[idx] = now

        changed = False
        for idx, wanted_pairs in sorted(need.items()):
            if idx not in m_chunks:
                print(f"  !! {fn} 区块 #{idx} 主存档不存在，跳过"); continue
            if idx not in c_chunks:
                print(f"  !! {fn} 区块 #{idx} 备份不存在，跳过"); continue

            name, mroot = chunk_parse(*m_chunks[idx], debug=debug)
            _, croot = chunk_parse(*c_chunks[idx], debug=debug)
            if mroot is None or croot is None:
                print(f"  !! {fn} 区块 #{idx} 解析失败，跳过"); continue

            bhost, cents = get_entities_list(croot)
            if cents is None:
                continue
            _, ments = get_entities_list(mroot)
            if ments is None:
                # 主存档该区块没有实体列表：新建。键名拼写与宿主位置必须
                # 沿用备份区块的实际结构——21w43a+ 地形区块是根级小写
                # entities，写错大小写游戏会静默忽略整批实体（而工具自身
                # 读取时大小写不敏感，自测根本发现不了）。
                bkey = next((k for k, v in bhost.v if v is cents), None)
                mhost = mroot if bhost is croot else (tag_get(mroot, "Level") or mroot)
                ments = Node(9, (10, []))
                mhost.v.append((bkey or "Entities", ments))

            added = []
            for ent in list(cents.v[1]):  # 快照遍历，防止边遍历边追加
                et = entity_type(ent)
                u = entity_uuid(ent)
                if u is None or (et, u) not in wanted_pairs:
                    continue
                if (et, u) in m_uuid or (et, u) in all_uuids:
                    continue  # 全存档 + 本次追加双重查重
                ments.v[1].append(ent)
                all_uuids.add((et, u))
                added.append((et, u))

            if added:
                raw = write_root(name, mroot)
                m_chunks[idx] = [2, zlib.compress(raw)]
                m_stamps[idx] = int(time.time())
                changed = True
                cnt = {}
                for et, _u in added:
                    cnt[et] = cnt.get(et, 0) + 1
                print(f"  {fn} 区块 #{idx}: 恢复 {cnt}")
        if changed:
            os.makedirs(os.path.dirname(main_path), exist_ok=True)
            save_region(main_path, m_chunks, m_stamps)
            print(f"已写回 {main_path}")

    print(f"\n完成。建议运行校验：python3 {PROG} verify {main_root} {backup_root}")
    return 0


def cmd_recover(args):
    main_root, backup_root = args.main, args.backup
    types_wanted = set(args.types.split(",")) if args.types else None
    skip_items = not args.include_items

    targets, stats, m_uuid = plan_recovery(main_root, backup_root, types_wanted,
                                           skip_items,
                                           verbose=args.verbose, debug=args.debug)

    # 用户显式指定区块时，只处理这些（全局区块坐标 "x,z;x,z"）
    if args.chunks:
        try:
            want = _parse_chunks_filter(args.chunks)
        except ValueError:
            print(f"错误: 无法解析 --chunks {args.chunks!r}（示例: \"3,2;3,3\"）",
                  file=sys.stderr)
            return 2
        targets = {k: v for k, v in targets.items() if (k[2], k[3]) in want}
        stats["missing"] = sum(len(v) for v in targets.values())

    _print_recovery_plan(targets, stats)
    if not targets:
        return 0

    if not args.apply:
        print("\n[dry-run] 未写盘。确认无误后加 --apply 执行。")
        return 0

    backup_dir = args.backup_dir or os.path.join(
        os.path.dirname(os.path.abspath(main_root.rstrip("/"))),
        "mcrescue-backup-{}".format(time.strftime("%Y%m%d-%H%M%S")))
    execute_recovery(main_root, backup_root, targets, m_uuid, backup_dir,
                     verbose=args.verbose, debug=args.debug)
    return 0


def cmd_verify(args):
    print("索引备份存档 ...")
    b_uuid, _, _ = index_entities(args.backup, verbose=args.verbose, debug=args.debug)
    print("索引主存档 ...")
    m_uuid, _, m_dups = index_entities(args.main, verbose=args.verbose, debug=args.debug)

    skip = set() if args.include_items else DEFAULT_SKIP_TYPES
    wanted = set(args.types.split(",")) if args.types else None

    # 1. 备份实体缺失情况
    missing = {}
    for (et, u), key in b_uuid.items():
        if et in skip:
            continue
        if wanted is not None and et not in wanted:
            continue
        if (et, u) not in m_uuid:
            missing.setdefault(et, []).append(key)
    # 2. 主存档重复 UUID（同一实体出现在多处，index 阶段已收进 dup_map）
    dups = [(et, u, keys) for (et, u), keys in m_dups.items()
            if wanted is None or et in wanted]

    n_b = sum(1 for (et, u) in b_uuid
              if et not in skip and (wanted is None or et in wanted))
    n_m = sum(1 for (et, u) in m_uuid
              if et not in skip and (wanted is None or et in wanted))
    print(f"\n备份实体(去重) : {n_b}")
    print(f"主存档实体(去重): {n_m}")
    print(f"主存档重复 UUID : {len(dups)}")
    for et, u, keys in dups[:20]:
        shown = " 与 ".join(str(k) for k in dict.fromkeys(keys))
        print(f"  重复 {et} {u} -> {shown}")
    n_missing = sum(len(v) for v in missing.values())
    print(f"备份有主存档没有: {n_missing}")
    for et, keys in sorted(missing.items()):
        print(f"  {et:<32} x{len(keys)}   例: {keys[0]}")

    ok = (n_missing == 0 and not dups)
    print("\n结果: %s" % ("通过 ✓" if ok else "存在问题"))
    return 0 if ok else 1


# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Minecraft Java 版存档实体抢救工具（零依赖）")
    sub = p.add_subparsers(dest="cmd", required=True)

    # 扫描类子命令共用的诊断开关（info/scan/recover/verify 都会索引实体）
    diag = argparse.ArgumentParser(add_help=False)
    diag.add_argument("-v", "--verbose", action="store_true",
                      help="打印扫描跳过统计（不可读区域/坏区块/启发式跳过等）")
    diag.add_argument("--debug", action="store_true",
                      help="非预期异常（疑似代码 bug）原样抛出，而不是当作坏数据跳过")

    sp = sub.add_parser("info", parents=[diag], help="查看存档信息与实体统计")
    sp.add_argument("save")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("where", help="方块坐标换算区块/区域文件")
    sp.add_argument("block", help='方块坐标，如 "63,58,45" 或 "63 58 45"（空格会自动转逗号）')
    sp.add_argument("--save", help="存档路径（给出则顺带查区块是否已生成）")
    sp.set_defaults(func=cmd_where)

    sp = sub.add_parser("scan", parents=[diag], help="列出备份有、主存档没有的实体")
    sp.add_argument("main", help="主存档路径")
    sp.add_argument("backup", help="备份存档路径")
    sp.add_argument("--types", help="只看指定实体类型（逗号分隔，如 minecraft:villager）")
    sp.add_argument("--include-items", action="store_true", help="把掉落物也计入（默认忽略）")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("recover", parents=[diag], help="把备份中缺失的实体合并回主存档")
    sp.add_argument("main", help="主存档路径")
    sp.add_argument("backup", help="备份存档路径")
    sp.add_argument("--types", help="只恢复指定实体类型（逗号分隔）")
    sp.add_argument("--chunks", help='限定区块，如 "3,2;3,3"（全局区块坐标）')
    sp.add_argument("--include-items", action="store_true", help="恢复掉落物（默认忽略）")
    sp.add_argument("--apply", action="store_true", help="真正写盘（不加则只预览）")
    sp.add_argument("--backup-dir", help="原文件备份目录（默认存档同级 mcrescue-backup-时间戳）")
    sp.set_defaults(func=cmd_recover)

    sp = sub.add_parser("verify", parents=[diag], help="校验恢复结果：无缺失、无重复")
    sp.add_argument("main")
    sp.add_argument("backup")
    sp.add_argument("--types", help="只校验指定实体类型（逗号分隔）")
    sp.add_argument("--include-items", action="store_true")
    sp.set_defaults(func=cmd_verify)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # 允许 where 的坐标用空格："63 58 45"
    if argv and argv[0] == "where" and len(argv) > 1 and " " in argv[1] and "," not in argv[1]:
        argv[1] = argv[1].replace(" ", ",")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
