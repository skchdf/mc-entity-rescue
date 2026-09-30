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


def chunk_nbt(cx, cz, villagers, sentinel):
    """构造区块 NBT：方块数据用哨兵数组模拟（恢复后必须原样保留）。"""
    blocks = Node(12, [sentinel] * 8)  # 模拟 BlockStates
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


def write_world(root_dir, chunks):
    """chunks: {(gx, gz): (villagers, sentinel)} -> 写出 region/r.0.0.mca"""
    region = {}
    for (gx, gz), (vills, sentinel) in chunks.items():
        idx = (gx & 31) + (gz & 31) * 32
        raw = write_root("", chunk_nbt(gx, gz, vills, sentinel))
        region[idx] = [2, zlib.compress(raw)]
    rdir = os.path.join(root_dir, "region")
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
    with contextlib.redirect_stdout(buf):
        code = mcrescue.main(list(argv))
    return code, buf.getvalue()


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

    def test_where_coord_math(self):
        self.assertEqual(parse_chunk_coord(63, 45), (3, 2, 0, 0, 67))
        self.assertEqual(parse_chunk_coord(-1, -1), (-1, -1, -1, -1, 31 + 31 * 32))


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
        code, out = run_cli("scan", self.main, self.backup,
                            "--types", "minecraft:villager")
        self.assertEqual(code, 0)
        self.assertIn("3 只实体缺失", out)
        self.assertIn("区块(3, 2)", out)

        # 2. dry-run 不写盘
        before = read_world(self.main)
        code, out = run_cli("recover", self.main, self.backup)
        self.assertEqual(code, 0)
        self.assertIn("dry-run", out)
        self.assertEqual(read_world(self.main), before)

        # 3. --apply 恢复
        bak_dir = os.path.join(self.tmp, "bak")
        code, out = run_cli("recover", self.main, self.backup,
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
        code, out = run_cli("verify", self.main, self.backup,
                            "--types", "minecraft:villager")
        self.assertEqual(code, 0)
        self.assertIn("通过", out)

        # 5. 再次 recover 应显示无需修改（幂等）
        code, out = run_cli("recover", self.main, self.backup)
        self.assertEqual(code, 0)
        self.assertIn("无需任何修改", out)

    def test_info(self):
        code, out = run_cli("info", self.main)
        self.assertEqual(code, 0)
        self.assertIn("TestWorld", out)
        self.assertIn("1.16.5", out)

    def test_chunk_filter(self):
        code, out = run_cli("recover", self.main, self.backup,
                            "--chunks", "3,2")
        self.assertEqual(code, 0)
        self.assertIn("(3,2)", out)      # 只计划了指定区块
        self.assertNotIn("(3,3)x", out)


if __name__ == "__main__":
    unittest.main()
