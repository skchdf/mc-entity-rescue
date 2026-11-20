#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcrescue 自测：用合成存档跑通 scan -> recover --apply -> verify 全流程。

不依赖 Minecraft，纯标准库，`python3 -m unittest` 即可运行。
"""

import contextlib
import gzip
import io
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mcrescue
from mcrescue import (
    Node, write_root, parse_root, save_region, load_region,
    entity_uuid, entities_empty, parse_chunk_coord, decompress,
)

DATA_VERSION = 2586  # 1.16.5


def villager(uid, prof="minecraft:librarian"):
    """构造一个带 UUID / 职业 / 位置的村民实体 NBT。"""
    return Node(10, [
        ("id", Node(8, "minecraft:villager")),
        ("UUID", Node(11, list(uid))),
        ("Pos", Node(9, (6, [Node(6, 0.0), Node(6, 64.0), Node(6, 0.0)]))),
        ("VillagerData", Node(10, [
            ("profession", Node(8, prof)),
            ("level", Node(3, 1)),
            ("type", Node(8, "minecraft:plains")),
        ])),
    ])


def legacy_villager(uid, prof="minecraft:librarian"):
    """同一只村民的旧版存法：UUID 拆成 UUIDMost / UUIDLeast 两个 TAG_Long
    （UUID = Most<<64 | Least）。"""
    def to_signed64(x):
        return struct.unpack(">q", struct.pack(">Q", x))[0]
    most = to_signed64((uid[0] << 32) | uid[1])
    least = to_signed64((uid[2] << 32) | uid[3])
    return Node(10, [
        ("id", Node(8, "minecraft:villager")),
        ("UUIDMost", Node(4, most)),
        ("UUIDLeast", Node(4, least)),
        ("Pos", Node(9, (6, [Node(6, 0.0), Node(6, 64.0), Node(6, 0.0)]))),
        ("VillagerData", Node(10, [
            ("profession", Node(8, prof)),
            ("level", Node(3, 1)),
            ("type", Node(8, "minecraft:plains")),
        ])),
    ])


def chunk_nbt(cx, cz, villagers, sentinel):
    """构造区块 NBT：方块数据用哨兵数组模拟（恢复后必须原样保留）。"""
    blocks = Node(12, [sentinel] * 8)  # 模拟 BlockStates（TAG_Long_Array）
    return Node(10, [
        ("DataVersion", Node(3, DATA_VERSION)),
        ("xPos", Node(3, cx)),
        ("zPos", Node(3, cz)),
        ("Level", Node(10, [
            ("Status", Node(8, "full")),
            ("Sections", Node(9, (10, [
                Node(10, [
                    ("Y", Node(1, 4)),
                    ("BlockStates", blocks),
                ]),
            ]))),
            ("Entities", Node(9, (10, list(villagers)))),
        ])),
    ])


def write_world(root_dir, chunks, subdir="region"):
    """chunks: {(gx, gz): (villagers, sentinel)} -> 写出 <subdir>/r.0.0.mca"""
    region = {}
    for (gx, gz), (vills, sentinel) in chunks.items():
        idx = (gx & 31) + (gz & 31) * 32
        raw = write_root("", chunk_nbt(gx, gz, vills, sentinel))
        region[idx] = [2, zlib.compress(raw)]
    rdir = os.path.join(root_dir, subdir)
    os.makedirs(rdir, exist_ok=True)
    stamps = {i: 1700000000 for i in region}
    save_region(os.path.join(rdir, "r.0.0.mca"), region, stamps)
    # 最小 level.dat
    level = Node(10, [("Data", Node(10, [
        ("LevelName", Node(8, "TestWorld")),
        ("DataVersion", Node(3, DATA_VERSION)),
        ("SpawnX", Node(3, 0)), ("SpawnY", Node(3, 64)), ("SpawnZ", Node(3, 0)),
    ]))])
    with gzip.open(os.path.join(root_dir, "level.dat"), "wb") as f:
        f.write(write_root("", level))


def read_world(root_dir):
    """读回所有区块 -> {(gx, gz): (实体UUID列表, 哨兵值)}"""
    out = {}
    path = os.path.join(root_dir, "region", "r.0.0.mca")
    chunks, _ = load_region(path)
    for idx, (comp, payload) in chunks.items():
        _, root = parse_root(decompress(comp, payload))
        level = mcrescue.tag_get(root, "Level")
        ents = mcrescue.tag_get(level, "Entities")
        uuids = sorted(entity_uuid(e) for e in ents.v[1])
        sections = mcrescue.tag_get(level, "Sections")
        sec0 = sections.v[1][0]
        sentinel = mcrescue.tag_get(sec0, "BlockStates").v[0]
        out[(idx % 32, idx // 32)] = (uuids, sentinel)
    return out


def run_cli(*argv):
    buf = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        code = mcrescue.main(list(argv))
    return code, buf.getvalue(), err.getvalue()


UIDS = [
    (1, 2, 3, 4), (5, 6, 7, 8), (9, 10, 11, 12), (13, 14, 15, 16),
]


class TestNbtRoundtrip(unittest.TestCase):
    def test_write_parse_write_identical(self):
        root = chunk_nbt(3, 2, [villager(UIDS[0])], 42)
        raw1 = write_root("", root)
        _, parsed = parse_root(raw1)
        raw2 = write_root("", parsed)
        self.assertEqual(raw1, raw2)

    def test_entities_empty_detection(self):
        empty = write_root("", chunk_nbt(0, 0, [], 1))
        self.assertTrue(entities_empty(empty))
        nonempty = write_root("", chunk_nbt(0, 0, [villager(UIDS[0])], 1))
        self.assertFalse(entities_empty(nonempty))

    def test_entities_empty_absent_key_is_empty(self):
        # 1.18+ 完整生成的地形区块通常连实体键都没有：应判空而不是保守解析
        raw = write_root("", Node(10, [
            ("DataVersion", Node(3, DATA_VERSION)),
            ("Status", Node(8, "full")),
        ]))
        self.assertTrue(entities_empty(raw))

    def test_entities_empty_lowercase_key(self):
        # 21w43a+ 生成中的区块：根级小写 entities
        raw = bytearray()
        raw += b"\x0a\x00\x00"
        raw += b"\x09\x00\x08entities" + b"\x0a" + struct.pack(">i", 1)
        raw += write_root("", villager(UIDS[0]))[len(b"\x0a\x00\x00"):]
        raw += b"\x00"
        self.assertFalse(entities_empty(bytes(raw)))
        empty_lc = b"\x0a\x00\x00\x09\x00\x08entities\x00\x00\x00\x00\x00\x00"
        self.assertTrue(entities_empty(empty_lc))

    def test_non_bmp_string_surrogate_pair_roundtrip(self):
        # Java modified UTF-8：U+1F600 拆成两个代理项各 3 字节
        emoji_b = b"\xed\xa0\xbd\xed\xb8\x80"
        raw = bytearray()
        raw += b"\x0a\x00\x00"                              # 根 compound ""
        raw += b"\x08\x00\x04name"
        raw += struct.pack(">H", len(emoji_b)) + emoji_b    # TAG_String
        raw += b"\x00"
        _, root = parse_root(bytes(raw))
        self.assertEqual(mcrescue.tag_get(root, "name").v, "\ud83d\ude00")
        raw2 = write_root("", root)
        self.assertEqual(bytes(raw), raw2)

    def test_corrupt_nbt_raises_not_silently_truncates(self):
        # byte array 声称 10 字节但数据只有 2 字节：必须报错而不是静默截断
        raw = bytearray()
        raw += b"\x0a\x00\x00"
        raw += b"\x07\x00\x02ba" + struct.pack(">i", 10) + b"\x01\x02"
        raw += b"\x00"
        with self.assertRaises(Exception):
            parse_root(bytes(raw))

    def test_where_coord_math(self):
        self.assertEqual(parse_chunk_coord(63, 45), (3, 2, 0, 0, 67))
        self.assertEqual(parse_chunk_coord(-1, -1), (-1, -1, -1, -1, 31 + 31 * 32))


class TestEntityUuid(unittest.TestCase):
    def test_legacy_two_long_matches_int_array(self):
        # 1.16 前的 UUIDMost/UUIDLeast 与 1.16+ 的 int 数组是同一身份：
        # UUID = Most<<64 | Least = i0<<96 | i1<<64 | i2<<32 | i3
        legacy = legacy_villager(UIDS[0])
        modern = villager(UIDS[0])
        self.assertEqual(entity_uuid(modern), (1, 2, 3, 4))
        self.assertEqual(entity_uuid(legacy), (1, 2, 3, 4))
        self.assertEqual(entity_uuid(legacy), entity_uuid(modern))

    def test_legacy_negative_long(self):
        # 128 位中高 32 位 >= 0x80000000 时 TAG_Long 是负数，归一化要还原无符号
        legacy = Node(10, [
            ("UUIDMost", Node(4, struct.unpack(">q", struct.pack(
                ">Q", (0xFFFFFFFF << 32) | 2))[0])),
            ("UUIDLeast", Node(4, struct.unpack(">q", struct.pack(
                ">Q", (0xFFFFFFFF << 32) | 2))[0])),
        ])
        self.assertEqual(entity_uuid(legacy),
                         (0xFFFFFFFF, 2, 0xFFFFFFFF, 2))

    def test_int_array_normalized_unsigned(self):
        # int 数组元素也可能为负（有符号 32 位），归一化成无符号段
        ent = Node(10, [("UUID", Node(11, [-2147483648, 0, 0, -1]))])
        self.assertEqual(entity_uuid(ent),
                         (0x80000000, 0, 0, 0xFFFFFFFF))


class TestRegionIO(unittest.TestCase):
    def _write(self, entries, name="r.0.0.mca"):
        tmp = tempfile.mkdtemp(prefix="mcrescue-region-")
        p = os.path.join(tmp, name)
        reg = {idx: val for idx, val in entries.items()}
        save_region(p, reg, {i: 123 for i in reg})
        return tmp, p

    def test_truncated_file_partial(self):
        tmp, p = self._write({
            5: [2, zlib.compress(write_root("", chunk_nbt(0, 0, [villager(UIDS[0])], 7)))],
            6: [2, zlib.compress(write_root("", chunk_nbt(1, 0, [villager(UIDS[1])], 7)))],
        })
        with open(p, "rb") as f:
            data = f.read()
        with open(p, "wb") as f:
            f.write(data[:len(data) // 2])
        chunks, stamps = load_region(p)  # 不应抛异常
        self.assertIsInstance(chunks, dict)
        self.assertTrue(all(len(v) == 2 for v in chunks.values()))
        shutil.rmtree(tmp, ignore_errors=True)

    def test_header_entry_beyond_eof(self):
        # 头表指向文件之外：不能崩，跳过该槽位
        tmp = tempfile.mkdtemp(prefix="mcrescue-region-")
        p = os.path.join(tmp, "r.0.0.mca")
        header = bytearray(8192)
        struct.pack_into(">I", header, 4 * 3, (100 << 8) | 1)  # 扇区 100 不存在
        with open(p, "wb") as f:
            f.write(bytes(header))
        chunks, _ = load_region(p)
        self.assertEqual(chunks, {})
        shutil.rmtree(tmp, ignore_errors=True)

    def test_zero_length_field(self):
        # length=0 的损坏头：不能 f.read(-1) 吞掉整个文件，也不能崩
        tmp = tempfile.mkdtemp(prefix="mcrescue-region-")
        p = os.path.join(tmp, "r.0.0.mca")
        header = bytearray(8192)
        struct.pack_into(">I", header, 4 * 3, (2 << 8) | 1)
        with open(p, "wb") as f:
            f.write(bytes(header))
            f.write(struct.pack(">I", 0))  # length = 0（损坏）
            f.write(b"\x00" * 4096)
        chunks, _ = load_region(p)
        self.assertEqual(chunks, {})
        shutil.rmtree(tmp, ignore_errors=True)

    def test_oversized_chunk_uses_external_mcc(self):
        # >255 扇区（约 1MiB）的区块：写外部 c.x.z.mcc，读回一致
        raw = write_root("", Node(10, [
            ("Entities", Node(9, (10, []))),
            ("Blob", Node(7, os.urandom(1_100_000))),
        ]))
        tmp, p = self._write({9: [3, raw]})
        mcc = os.path.join(tmp, "c.9.0.mcc")  # 局部 (x=9, z=0)
        self.assertTrue(os.path.exists(mcc))
        chunks, _ = load_region(p)
        self.assertIn(9, chunks)
        self.assertEqual(chunks[9], [3, raw])  # 压缩类型 3：数据原样
        shutil.rmtree(tmp, ignore_errors=True)

    def test_region_roundtrip_preserves_bytes(self):
        raw = write_root("", chunk_nbt(1, 1, [villager(UIDS[0])], 99))
        tmp, p = self._write({33: [2, zlib.compress(raw)]})
        chunks, stamps = load_region(p)
        self.assertEqual(decompress(*chunks[33]), raw)
        self.assertEqual(stamps[33], 123)
        shutil.rmtree(tmp, ignore_errors=True)


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mcrescue-test-")
        self.main = os.path.join(self.tmp, "World")
        self.backup = os.path.join(self.tmp, "World-backup")
        # 备份：(3,2) 有 2 只村民, (3,3) 有 2 只
        write_world(self.backup, {
            (3, 2): ([villager(UIDS[0]), villager(UIDS[1])], 111),
            (3, 3): ([villager(UIDS[2]), villager(UIDS[3])], 222),
        })
        # 主存档：(3,2) 村民全没了(被误杀), (3,3) 剩 1 只(UIDS[2])
        # ——但建筑施工改变了哨兵值
        write_world(self.main, {
            (3, 2): ([], 999),
            (3, 3): ([villager(UIDS[2])], 888),
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_flow(self):
        # 1. scan 找出缺失
        code, out, _ = run_cli("scan", self.main, self.backup,
                               "--types", "minecraft:villager")
        self.assertEqual(code, 0)
        self.assertIn("3 只实体缺失", out)
        self.assertIn("区块(3, 2)", out)

        # 2. dry-run 不写盘
        before = read_world(self.main)
        code, out, _ = run_cli("recover", self.main, self.backup)
        self.assertEqual(code, 0)
        self.assertIn("dry-run", out)
        self.assertEqual(read_world(self.main), before)

        # 3. --apply 恢复
        bak_dir = os.path.join(self.tmp, "bak")
        code, out, _ = run_cli("recover", self.main, self.backup,
                               "--types", "minecraft:villager",
                               "--apply", "--backup-dir", bak_dir)
        self.assertEqual(code, 0)
        self.assertIn("待恢复", out)
        self.assertIn(": 3", out)

        after = read_world(self.main)
        # 两个区块的村民都补齐了
        self.assertEqual(len(after[(3, 2)][0]), 2)
        self.assertEqual(len(after[(3, 3)][0]), 2)
        # 施工后的哨兵值原样保留（方块数据未被动过）
        self.assertEqual(after[(3, 2)][1], 999)
        self.assertEqual(after[(3, 3)][1], 888)
        # 没有重复 UUID
        all_uids = after[(3, 2)][0] + after[(3, 3)][0]
        self.assertEqual(len(all_uids), len(set(all_uids)))
        # 原文件有备份
        self.assertTrue(os.path.exists(os.path.join(bak_dir, "region", "r.0.0.mca")))

        # 4. verify 通过
        code, out, _ = run_cli("verify", self.main, self.backup,
                               "--types", "minecraft:villager")
        self.assertEqual(code, 0)
        self.assertIn("通过", out)

        # 5. 再次 recover 应显示无需修改（幂等）
        code, out, _ = run_cli("recover", self.main, self.backup)
        self.assertEqual(code, 0)
        self.assertIn("无需任何修改", out)

    def test_legacy_uuid_entity_not_duplicated(self):
        # 备份里的村民用旧版 UUIDMost/UUIDLeast 存 UUID，主存档同一只村民
        # 已是 int 数组 UUID：归一化后应判定"已存在"，绝不复制
        tmp = tempfile.mkdtemp(prefix="mcrescue-legacy-")
        try:
            main = os.path.join(tmp, "World")
            backup = os.path.join(tmp, "World-backup")
            write_world(main, {(3, 2): ([villager(UIDS[0])], 10)})
            write_world(backup, {(3, 2): ([legacy_villager(UIDS[0])], 11)})
            code, out, _ = run_cli("recover", main, backup)
            self.assertEqual(code, 0)
            self.assertIn("无需任何修改", out)
            self.assertIn("主存档已存在（跳过）: 1", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_info(self):
        code, out, _ = run_cli("info", self.main)
        self.assertEqual(code, 0)
        self.assertIn("TestWorld", out)
        self.assertIn("1.16.5", out)

    def test_chunk_filter(self):
        code, out, _ = run_cli("recover", self.main, self.backup,
                               "--chunks", "3,2")
        self.assertEqual(code, 0)
        self.assertIn("(3,2)", out)      # 只计划了指定区块
        self.assertNotIn("(3,3)x", out)

    def test_chunk_filter_bad_input(self):
        code, out, err = run_cli("recover", self.main, self.backup,
                                 "--chunks", "3,x")
        self.assertEqual(code, 2)
        self.assertIn("无法解析 --chunks", err)

    def test_corrupt_chunk_does_not_crash_scan(self):
        # 一个坏 zlib 区块：扫描必须跳过它而不是整个崩掉
        reg = {}
        path = os.path.join(self.main, "region", "r.0.0.mca")
        chunks, stamps = load_region(path)
        chunks[10] = [2, b"definitely-not-a-zlib-stream"]
        stamps[10] = 1
        save_region(path, chunks, stamps)
        code, out, _ = run_cli("info", self.main)
        self.assertEqual(code, 0)
        self.assertIn("实体总数   : 1", out)  # (3,3) 的那只仍被统计到


class TestDuplicateDetection(unittest.TestCase):
    def test_verify_reports_duplicate_uuid(self):
        tmp = tempfile.mkdtemp(prefix="mcrescue-dup-")
        try:
            main = os.path.join(tmp, "World")
            backup = os.path.join(tmp, "World-backup")
            # 同一只村民的 UUID 同时出现在两个区块（复制人事故现场）
            write_world(main, {
                (3, 2): ([villager(UIDS[0])], 1),
                (3, 3): ([villager(UIDS[0])], 2),
            })
            write_world(backup, {(0, 0): ([], 0)})
            code, out, _ = run_cli("verify", main, backup,
                                   "--types", "minecraft:villager")
            self.assertEqual(code, 1)
            self.assertIn("主存档重复 UUID : 1", out)
            self.assertIn("重复 minecraft:villager (1, 2, 3, 4)", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_info_reports_duplicate_uuid(self):
        tmp = tempfile.mkdtemp(prefix="mcrescue-dup-")
        try:
            main = os.path.join(tmp, "World")
            write_world(main, {
                (3, 2): ([villager(UIDS[0])], 1),
                (3, 3): ([villager(UIDS[0])], 2),
            })
            code, out, _ = run_cli("info", main)
            self.assertEqual(code, 0)
            self.assertIn("重复 UUID  : 1", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestRegionLayout(unittest.TestCase):
    def test_custom_dimension_scanned(self):
        # 数据包/模组自定义维度：dimensions/<ns>/<dim>/region（深度不限）
        tmp = tempfile.mkdtemp(prefix="mcrescue-dim-")
        try:
            world = os.path.join(tmp, "World")
            write_world(world, {(0, 0): ([villager(UIDS[0])], 3)},
                        subdir="dimensions/mymod/cool_dim/region")
            code, out, _ = run_cli("info", world)
            self.assertEqual(code, 0)
            self.assertIn("实体总数   : 1", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_vanilla_261_layout_scanned(self):
        # 26.1+ 原版布局：dimensions/minecraft/overworld/region
        tmp = tempfile.mkdtemp(prefix="mcrescue-dim-")
        try:
            world = os.path.join(tmp, "World")
            write_world(world, {(0, 0): ([villager(UIDS[0])], 3)},
                        subdir="dimensions/minecraft/overworld/region")
            code, out, _ = run_cli("info", world)
            self.assertEqual(code, 0)
            self.assertIn("实体总数   : 1", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_recover_creates_missing_entities_file(self):
        # 1.17+：主存档还没有 entities/ 目录时，可以安全新建实体文件
        tmp = tempfile.mkdtemp(prefix="mcrescue-ent-")
        try:
            main = os.path.join(tmp, "World")
            backup = os.path.join(tmp, "World-backup")
            write_world(main, {}, subdir="region")  # 只有地形与 level.dat
            write_world(backup, {
                (3, 2): ([villager(UIDS[0]), villager(UIDS[1])], 5),
            }, subdir="entities")
            bak_dir = os.path.join(tmp, "bak")
            code, out, _ = run_cli("recover", main, backup, "--apply",
                                   "--backup-dir", bak_dir)
            self.assertEqual(code, 0)
            self.assertIn("将新建实体文件", out)
            ent_path = os.path.join(main, "entities", "r.0.0.mca")
            self.assertTrue(os.path.exists(ent_path))
            # 主存档地形完全没动（terrain 文件仍是空表）
            terrain, _ = load_region(os.path.join(main, "region", "r.0.0.mca"))
            self.assertEqual(terrain, {})
            # 实体文件里有 2 只村民
            chunks, _ = load_region(ent_path)
            _, root = parse_root(decompress(*chunks[67]))
            _, ents = mcrescue.get_entities_list(root)
            self.assertEqual(len(ents.v[1]), 2)
            # verify 通过
            code, out, _ = run_cli("verify", main, backup)
            self.assertEqual(code, 0)
            self.assertIn("通过", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_recover_refuses_to_create_terrain_region(self):
        # 主存档连 region 文件都没有 = 区块不存在：
        # 绝不能凭空创建（会把备份时的地形一并写进去）
        tmp = tempfile.mkdtemp(prefix="mcrescue-terrain-")
        try:
            main = os.path.join(tmp, "World")
            backup = os.path.join(tmp, "World-backup")
            os.makedirs(main)  # 主存档里连 region/ 目录都没有
            write_world(backup, {(3, 2): ([villager(UIDS[0])], 5)})
            code, out, _ = run_cli("recover", main, backup, "--apply",
                                   "--backup-dir", os.path.join(tmp, "bak"))
            self.assertEqual(code, 0)
            self.assertIn("地形文件缺失", out)
            # 不会凭空创建任何地形 region 文件
            self.assertFalse(os.path.exists(os.path.join(main, "region")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
