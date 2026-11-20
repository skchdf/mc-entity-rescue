# mc-entity-rescue

**零依赖的 Minecraft Java 版存档实体抢救工具** —— 从备份里把丢失的实体（村民、铁傀儡……）按 UUID 精准补回主存档，**不回滚你的建筑**。

```
备份的实体 + 你的新地形 = 两个都要
```

## 为什么需要它

常见事故：村民被僵尸感染后在和平模式被清空、`/kill @e` 误执行、区块被误改……

常规解法是**整块回滚**——用备份覆盖整个区块。但如果你在备份之后已经做了大量建筑，整块覆盖会把这些施工全部冲掉。

本工具的做法是**实体级合并**：实体和方块在区块 NBT 里是并列字段，只把备份中缺失的实体列表段落拼回当前区块，方块数据一个字节都不碰。

```
备份区块 NBT = [方块数据 ..., Entities: 9只村民]
你的区块 NBT = [方块数据 ...(你的施工)..., Entities: 0只]
                              ↓ 只拼回 Entities 这一段
最终 NBT     = [方块数据 ...(你的施工)..., Entities: 9只村民]
```

> 本工具源自一次真实抢救：1.16.5 生存档两处开放村庄的 18 只村民被误清，
> 而玩家在事发后已建造多时。结果 37/37 只村民全部归位、0 重复、施工完好。
> 完整过程见 [docs/03-case-study.md](docs/03-case-study.md)。

## 特性

- **纯标准库**：单文件 `mcrescue.py`，无需 pip 安装任何东西（Python ≥ 3.8）
- **UUID 全存档查重**：恢复前先在整个存档（含三个维度）里查 UUID，
  村民只是"散步到隔壁区块"也不会被复制出第二个
- **实体归实体，方块归方块**：只改 `Entities` 列表，地形 / 光照 / 箱子 / 床全部保留
- **默认 dry-run**：不加 `--apply` 只打印计划，绝不写盘
- **自动备份**：写盘前把原 `.mca` 拷贝到备份目录，写入用临时文件 + 原子替换
- **幂等**：跑两次 `recover`，第二次会告诉你"无需任何修改"
- **自动跳过掉落物**：掉落物 5 分钟自然消失，盲目恢复可能与你已捡起的物品重复

## 快速开始

```bash
# 1. 完全关闭 Minecraft！

# 2. 看看两个存档差了哪些实体（只读）
python3 mcrescue.py scan   /存档/World /存档/World-backup

# 3. 预览恢复计划（仍然只读，dry-run）
python3 mcrescue.py recover /存档/World /存档/World-backup

# 4. 确认无误，真正执行
python3 mcrescue.py recover /存档/World /存档/World-backup --apply

# 5. 校验：无缺失、无重复
python3 mcrescue.py verify  /存档/World /存档/World-backup
```

常用参数：

| 参数 | 说明 |
|------|------|
| `--types minecraft:villager` | 只处理指定实体（`scan` / `recover` / `verify` 均支持） |
| `--chunks "3,2;3,3"` | 限定全局区块坐标，其余不动 |
| `--include-items` | 把掉落物也纳入（默认忽略） |
| `--apply` | 真正写盘（`recover` 专用） |
| `--backup-dir DIR` | 指定原文件备份位置（默认存档同级 `mcrescue-backup-时间戳/`） |

辅助命令：

```bash
# F3 里的坐标 -> 区块 / 区域文件 / 表头索引
python3 mcrescue.py where "63 58 45" --save /存档/World

# 存档信息 + 全实体统计
python3 mcrescue.py info /存档/World
```

## 运行测试

```bash
python3 -m unittest discover -s tests
```

测试用合成存档跑通 `scan → recover --apply → verify` 全流程，不依赖 Minecraft。

## 兼容性

| 项目 | 状态 |
|------|------|
| Java 1.16.5（Anvil + 区块内 `Level.Entities`） | ✅ 实战验证（本工具的出处） |
| NBT 全部 13 种标签、gzip / zlib / 未压缩三种区块压缩 | ✅ 支持，写回保真 |
| 1.17+ 实体拆分到 `entities/` 目录（20w45a） | ✅ 代码支持（同为 Anvil 格式），未实战 |
| 1.18+ 去 `Level` 层、生成中区块键名 `entities` 小写（21w43a） | ✅ 代码支持，未实战 |
| 26.1+ `dimensions/minecraft/*` 新布局、数据包/模组自定义维度 | ✅ 代码支持，未实战 |
| 超 1 MiB 的大区块（外部 `c.x.z.mcc` 文件） | ✅ 代码支持，未实战 |
| 基岩版 | ❌ 不支持（格式完全不同） |

> 改存档前请自己先备份。工具自带的自动备份是第二道保险，不是第一道。

## 格式教程

想搞懂原理，或者以后自己写这类工具：

1. **[Anvil 区域文件格式](docs/01-anvil-format.md)** —— 8KB 头表、扇区分配、区块定位与安全重写
2. **[NBT 格式](docs/02-nbt-format.md)** —— 标签类型、大端序、往返保真为什么是关键
3. **[实战复盘：抢救 18 只村民](docs/03-case-study.md)** —— F3 坐标解读、定位、合并、验证全过程

## License

[MIT](LICENSE)
