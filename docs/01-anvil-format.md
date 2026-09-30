# 教程 1：Anvil 区域文件格式

Minecraft 把每 **32×32 = 1024 个区块**打包进一个 `.mca` 文件（region file），
文件名即坐标：`r.<区域X>.<区域Z>.mca`。本文讲清楚三件事：

1. 怎么从方块坐标定位到某个区块的字节
2. 头表怎么读
3. 怎么**安全地重写**一个区域文件

> 下文的代码都来自 [`mcrescue.py`](../mcrescue.py)，可以直接对照读。

---

## 1. 坐标换算链

```
方块 (63, 45)
   │  >> 4 （除以 16 向下取整）
区块 (3, 2)          ← 一个区块 = 16×16 方块列
   │  >> 5 （除以 32）
区域 (0, 0)          ← 文件名 r.0.0.mca
   │
文件内索引 = (区块X mod 32) + (区块Z mod 32) × 32 = 3 + 2×32 = 67
```

```python
def parse_chunk_coord(x, z):
    cx, cz = x >> 4, z >> 4
    rx, rz = cx >> 5, cz >> 5
    idx = (cx & 31) + (cz & 31) * 32
    return cx, cz, rx, rz, idx
```

负坐标也成立：`-1 >> 4 == -1`、`-1 & 31 == 31`（Python 的无穷位补码语义），
所以方块 `(-1, -1)` 落在区块 `(-1, -1)`、区域 `r.-1.-1.mca`、索引 `1023`。

游戏里的 F3 调试屏直接给了这条链的中间量，例如 1.16.5 显示：

```
Block:  63 58 45
Chunk:  15 10 13 in 3 3 2
```

- `15 10 13` = 方块在区块内的偏移（`63 & 15 = 15`，`58 & 15 = 10`，`45 & 15 = 13`）
- `3 3 2` = section 坐标（`63 >> 4 = 3`，`58 >> 4 = 3`，`45 >> 4 = 2`）

两组数字互为印证：**区块 X/Z = 第二组的 X/Z**。工具的 `where` 子命令做的就是这个换算。

---

## 2. 文件结构：8KB 头表 + 扇区数据

```
字节 0        ─┬─ 位置表（location table）4096 字节
字节 4096     ─┼─ 时间戳表（timestamp table）4096 字节
字节 8192     ─┴─ 区块数据，按 4096 字节扇区对齐
```

### 2.1 位置表

1024 个条目，每条 **4 字节**，按索引 `0..1023` 排列：

```
 3 字节  扇区号 offset   （× 4096 = 数据的绝对字节偏移）
 1 字节  扇区数 sectors  （该区块占用几个 4KB 扇区，最大 255）
```

```python
entry = struct.unpack(">I", header[i*4:i*4+4])[0]   # 大端 4 字节一次读出
offset = (entry >> 8) * 4096                         # 高 24 位是扇区号
sectors = entry & 0xFF                               # 低 8 位是扇区数
if offset == 0:
    ...  # 该区块不存在
```

**大端序（big-endian）贯穿整个格式**——NBT、头表、时间戳全是大端，
这是 Java 写二进制的习惯（`struct` 格式串里的 `>`）。

### 2.2 时间戳

第二张表同样是 1024 × 4 字节，记录每个区块**上次保存的 Unix 时间**。
纯元数据，游戏不依赖它，但 mcaselector 之类工具会用。

### 2.3 区块数据

每个区块在扇区里是：

```
4 字节   length        后续数据的总长度（含压缩类型字节）
1 字节   压缩类型      1=gzip, 2=zlib, 3=不压缩
其余     压缩后的 NBT
```

注意 `length` **包含**压缩类型那 1 字节，所以 NBT 实际长度是 `length - 1`。

```python
f.seek(offset)
length = struct.unpack(">I", f.read(4))[0]
comp = f.read(1)[0]
payload = f.read(length - 1)
raw = zlib.decompress(payload) if comp == 2 else payload
```

---

## 3. 安全重写

重写的诱惑是"原地覆写"——**不要这样做**。新的压缩数据比旧的长时会
踩坏后面的区块。正确做法是**整体重排**：

1. 读出全部 1024 个槽位的（压缩类型, 数据, 时间戳）
2. 替换要改的槽位
3. 顺序重新分配扇区号，重建头表
4. 写到 `.tmp`，`fsync` 后 `os.replace()` **原子替换**

```python
def save_region(path, chunks, stamps):
    header = bytearray(8192)
    body = bytearray()
    next_off = 2                      # 数据从扇区 2（字节 8192）开始
    for i in range(1024):
        if i not in chunks:
            continue
        comp, payload = chunks[i]
        data = struct.pack(">I", len(payload) + 1) + bytes([comp]) + payload
        sectors = (len(data) + 4095) // 4096      # 向上取整到扇区
        struct.pack_into(">I", header, i*4, (next_off << 8) | sectors)
        struct.pack_into(">I", header, 4096 + i*4, stamps.get(i, 0))
        body += data
        body += b"\x00" * (sectors * 4096 - len(data))   # 扇区内补零
        next_off += sectors
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(bytes(header)); f.write(bytes(body))
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)             # 原子替换，断电也不会留下半截文件
```

三个容易踩的坑：

- **扇区数上限 255**（1 字节装不下）。正常区块远达不到，除非你往一个区块塞了几万个实体。
- **0 字节 `.mca`** 真实存在（空维度、POI 目录里一堆），读头表时必须先检查
  `len(header) < 8192` 就跳过，否则 `struct.unpack` 会直接抛异常。
- **游戏运行时不要写**。Minecraft 关存档时会整块重写 `.mca`，你在它旁边
  写入的结果可能被覆盖，或者互相踩坏。`session.lock` 还在就别动。

---

## 4. 动手验证

```bash
# 区块 (3,2) 对应 r.0.0.mca 的第 67 号槽位
python3 mcrescue.py where "63 58 45" --save /你的存档/World
```

输出：

```
方块坐标   : 63,58,45
区块坐标   : (3, 2)   [方块范围 x 48..63, z 32..47]
区域文件   : r.0.0.mca
表头索引   : 67   (局部(3, 2))
区块状态   : 存在
```

---

下一步：[教程 2：NBT 格式](02-nbt-format.md) —— 区块数据解压出来之后长什么样。
