# 教程 2：NBT 格式

NBT（Named Binary Tag）是 Mojang 自定义的二进制树结构，区块、实体、
`level.dat` 全是它。本文讲清楚：

1. 标签类型与字节布局
2. 为什么解析器必须**保留类型**（往返保真）
3. 一个真实实体的字节级走读

---

## 1. 标签类型表

| 类型号 | 类型 | Python 表示 |
|-------|------|------------|
| 0 | TAG_End（compound 结束符） | — |
| 1 | TAG_Byte | `int` |
| 2 | TAG_Short | `int` |
| 3 | TAG_Int | `int` |
| 4 | TAG_Long | `int` |
| 5 | TAG_Float | `float` |
| 6 | TAG_Double | `float` |
| 7 | TAG_Byte_Array | `bytes` |
| 8 | TAG_String | `str` |
| 9 | TAG_List（同类型元素） | `(元素类型, [Node, ...])` |
| 10 | TAG_Compound | `[(名称, Node), ...]` |
| 11 | TAG_Int_Array | `list[int]` |
| 12 | TAG_Long_Array | `list[int]` |

**所有整数都是大端序**。字符串是「UTF-8 + 2 字节无符号长度前缀」，
没有结尾的 `\0`。

## 2. 结构布局

**命名标签（named tag）**——除 List 元素外，每个标签都带名字：

```
1 字节        标签类型
2 字节        名称长度 N
N 字节        名称（UTF-8）
...           值（payload，结构取决于类型）
```

**Compound（类型 10）** 的值 = 一串命名标签，直到 `0x00` 结束：

```
[命名标签][命名标签]...[0x00]
```

**List（类型 9）** 的值 = **元素没有名字**：

```
1 字节   元素类型
4 字节   元素个数
...      连续的纯值（不带类型字节、不带名称）
```

**文件本身**就是一个命名标签（根标签），名称通常是空字符串。
所以一条最简单的 NBT 是：

```
0A          根是 compound
00 00       名称长度 0
00          空 compound，立即结束
```

——总共 4 个字节。

## 3. 往返保真为什么是关键

抢救存档时，**解析 → 修改 → 写回** 这条链上，未修改的部分必须字节级还原，
否则轻则体积膨胀，重则丢数据。三个经典陷阱：

### 陷阱 1：把 Byte 当 Int

`TAG_Byte(1)` 和 `TAG_Int(1)` 值一样、语义不同。如果解析时统一成 `int`
再写回时猜类型，`HasPhysics`、`isVirtual` 这类字段就会被改型。
**解法：类型随值一起存。** 本工具的 `Node` 同时携带 `t`（类型号）和 `v`（值）：

```python
class Node:
    __slots__ = ("t", "v")
    def __init__(self, t, v):
        self.t = t      # 标签类型 1-12，写回时按它还原
        self.v = v
```

### 陷阱 2：Compound 条目顺序被打乱

Mojang 的解析器不挑顺序，但**按字节比对存档**的工具会挑。
Python 3.7+ 的 dict 保序，本工具直接用 `[(name, Node), ...]` 列表存 Compound，
插入新条目时 `append` 到末尾，不重排。

### 陷阱 3：浮点精度

`struct.unpack` 出来的 `float` 是 Python 双精度，写回 `struct.pack(">f")`
会先截断成单精度——round-trip 是安全的（32→64→32 无损），但**别**中途
拿它做算术运算再写回。

验证往返保真的自测（`tests/test_mcrescue.py`）：

```python
def test_write_parse_write_identical(self):
    raw1 = write_root("", root)
    _, parsed = parse_root(raw1)
    raw2 = write_root("", parsed)
    self.assertEqual(raw1, raw2)      # 解析两次写出的字节完全一致
```

## 4. 字节级走读：一个村民实体

下面是本工具合成测试里的村民（截取关键字段）：

```
0A 00 00                        根 compound，名称空
   08 00 02 69 64               TAG_String "id"
      00 11 6D 69 ...           长度 17, "minecraft:villager"
   0B 00 04 55 55 49 44         TAG_Int_Array "UUID"
      00 00 00 04               4 个 int
      00 00 00 01 00 00 00 02 00 00 00 03 00 00 00 04
   0A 00 0B 56 69 6C 6C ...     TAG_Compound "VillagerData"
      08 00 0B 70 72 6F ...     TAG_String "profession"
         00 11 6D 69 ...        "minecraft:librarian"
      ...
   00                            compound 结束
```

几个值得注意的点：

- **实体身份 = UUID**。`UUID`（int[4]，1.9+）或老的 `UUIDMost`/`UUIDLeast`
  （两个 Long）。本工具两种都认——这是全存档查重防复制的依据。
- **实体类型 = `id` 字符串**（`minecraft:villager`），1.7.10 及以前是数字。
- **村民的职业 / 等级 / 类型**打包在 `VillagerData` compound，
  **交易数据**在 `Offers.Recipes` 列表——这些都随实体整体搬运，
  所以"实体级合并"能连交易记录一起救回来。
- `Pos`、`Rotation`、`Motion` 是 float 数组，实体被"搬"到主存档后
  站在它**备份时**的位置上。

## 5. gzip 与 zlib 的分工

| 文件 | 容器 | 说明 |
|------|------|------|
| `level.dat` | **gzip**（`1F 8B` 开头） | 独立小文件 |
| 区块数据 | **zlib**（默认，压缩类型 2） | 长度前缀内 |
| 极少数区块 | gzip（类型 1）/ 不压缩（类型 3） | 历史遗留，都得会读 |

```python
def decompress(comp, payload):
    if comp == 2: return zlib.decompress(payload)
    if comp == 1: return gzip.decompress(payload)
    return payload
```

## 6. 性能小抄

5 万区块逐个解析在 Python 里不便宜（~85 秒）。但绝大多数区块实体列表是
空的，而 NBT 里 List 标签的字节是固定的：

```
09 00 08 45 6E 74 69 74 69 65 73   类型9 + 长度8 + "Entities"
00                                  元素类型（空表为 0）
00 00 00 00                         元素个数 0
```

直接在**解压后的字节**上找这个模式、确认计数为 0 就跳过整块解析，
实测把全量扫描从 85 秒降到 23 秒（4 倍多）：

```python
_ENT_TAG = b"\x09\x00\x08Entities"

def entities_empty(raw):
    start, seen = 0, False
    while True:
        i = raw.find(_ENT_TAG, start)
        if i < 0:
            return seen        # 见过且全空 -> 跳过；没见过 -> 保守解析
        seen = True
        p = i + len(_ENT_TAG)
        if p + 5 > len(raw) or int.from_bytes(raw[p+1:p+5], "big") != 0:
            return False       # 有非空列表，必须完整解析
        start = i + 1
```

**优化必须偏保守**：任何拿不准的情况都返回"需要解析"，宁可慢也不能漏。
模式可能在方块数据里碰巧出现，所以要检查**所有**出现位置，全部为空才跳过。

---

下一步：[教程 3：实战复盘](03-case-study.md) —— 用这两个格式知识抢救真实存档。
