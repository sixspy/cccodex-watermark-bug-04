# 双影处置复现步骤（供二验）

对象：`cc-watermark-lab`（血缘水印台）· 货号 `watermark-bug-04`
库内同一运行码出现两枚印记（**双影**）→ 差额台报警 → 去重入库失败。

本纸只写**怎么复现、怎么验**；两边各验一遍：差额一遍、去重一遍。

---

## 0. 起服务

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q          # 应 14 passed
DATABASE_PATH=/tmp/wm2.db PORT=7530 .venv/bin/python run.py
```

浏览器开 http://127.0.0.1:7530/login ，`admin / 123456`。
底栏七格：模板 · 批次 · 冲突 · 印记 · 去重 · 追索 · 缺口。

**种子库自带的双影**：运行码 `pay-20261115-01`，两枚印记

| 序 | 来路 | 印记值 | 状态 |
|----|------|--------|------|
| 1 | 先手 | `WM-PAY-151-01` | 冲突待处置 |
| 2 | 后手 | `WM-PAY-151-02` | 冲突待处置 |

---

## 1. 四处顺着核（初验：双影确实在四处同时可见）

| 处 | 怎么看 | 期望 |
|----|--------|------|
| 冲突目录 | 底栏「冲突」 | `pay-20261115-01` 一张卡，两行印记（先手/后手），亮「待处置」，标「运行码与库存行同源」「差额台停等」 |
| 印记列表 | 底栏「印记」 | 该运行码两行，均为「冲突待处置」 |
| 批次卡片 | 底栏「批次」 | 该卡亮「印记 2 枚」「双影停等」，无「盖印」按钮 |
| 去重进度 | 底栏「去重」 | 一行：停等，**死信因**「双影待处置·死信因待定」，死信 0 枚 |

命令行等价核对：

```bash
B=http://127.0.0.1:7530; C="-b /tmp/c.txt"
curl -s -c /tmp/c.txt -X POST $B/api/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"123456"}' >/dev/null

curl -s $C $B/api/conflicts | python3 -m json.tool
curl -s $C $B/api/marks | python3 -m json.tool
curl -s $C $B/api/batch-cards | python3 -m json.tool
curl -s $C $B/api/dedup-progress | python3 -m json.tool
```

**差额台报警**（未处置时）：

```bash
curl -s $C -X POST $B/api/gap-reports -H 'Content-Type: application/json' \
  -d '{"as_of_date":"2026-11-16"}' | python3 -m json.tool
# summary 应为「截至 2026-11-16：应盖印 5，已盖印 2，缺口 3（双影停等）」
# stopped=true，blocked_run_codes=["pay-20261115-01"]
# 注意：双影不参与计数，已盖印仍是 2（不是 3）
```

---

## 2. 双影处置：两种落库命运，选边改变留下哪枚

进「冲突」页，在 `pay-20261115-01` 卡里二选一，再点**合并**。

### 路线 A —— 部分留存先手，后手行整笔回滚送死信

1. 选「留存先手」→ 点「合并」。
2. 期望页上提示「合并结局：留存先手」；卡片保留芯片亮 **保留 `WM-PAY-151-01`**，印记变 1 行。
3. 「印记」页只剩先手一行，状态「有效」。
4. 「去重」页该码变为 **已完成**，死信 1 枚，死信因「留存先手印记，后手行整笔回滚入死信」；
   死信箱出现 `WM-PAY-151-02 · 后手`。
5. 「合并结局」表出现一行：`keep_first / 留存先手 / WM-PAY-151-01 / 死信 1 / 已完成`。

### 路线 B —— 两枚一并整笔回滚，由排队席重打唯一印记

1. 选「整笔回滚·待重打」→ 出现「重打水印值」输入框（默认 `WM-PAY-20261115-01-R1`，
   可直接改成别的）→ 点「合并」。
2. 期望提示「合并结局：整笔回滚·待重打」；卡片变「无有效印记·待重打」，批次卡片回「待盖印」、库存值清空。
3. 「去重」页该码 **待重打**，死信 **2** 枚；死信箱两行（先手 + 后手），死信因「两枚整笔回滚（排队重打）」。
4. 排队席出现一行 `待重打`，点 **重打唯一印记**。
5. 期望：该码变为 **已完成**，印记 1 枚（来路「重打」，值=目标值），库存值与它同值；
   排队席转「已重打」；合并结局转「已完成（重打）」且留下印记=目标值。

> 二验时两条路线各起一个干净库分别走（`DATABASE_PATH` 指不同文件），因为一个运行码只能处置一次。

---

## 3. 两遍验收

### 3.1 去重一遍

```bash
curl -s $C $B/api/verify/dedup | python3 -m json.tool
```

八项全 true 才算过：

- 双影清零 · 单一有效印记 · 运行码与库存行同源 · 库存值与有效印记同值
- 无隐形双影（meta 不再藏第二枚）· 死信计数与死信行一致
- 已处置即无待处置印记 · 双影停等清零 · 排队席已清空

（路线 B 在「重打」之前跑，应当 **不过**：排队席未清空。）

### 3.2 差额一遍

```bash
curl -s $C $B/api/verify/gap | python3 -m json.tool
curl -s $C -X POST $B/api/gap-reports -H 'Content-Type: application/json' \
  -d '{"as_of_date":"2026-11-16"}' | python3 -m json.tool
```

期望 `verify/gap.ok=true`，四项：已盖印行与有效印记一一对应 / 已盖印行都有有效印记 /
有效印记不越出已盖印行 / 差额不被双影污染。
对账 summary 变为「（存在缺口）」，`stopped=false`，已盖印仍为 2、缺口仍为 3。

**关键：处置前后已盖印数不变（2），说明双影从来没有被算进差额。**
旧版把 `meta` 里的隐形第二枚加进已盖印数，正是「差额台报警」的来源。

---

## 4. 反例（必须失败才算真修好）

### 4.1 只改计数不改删除逻辑 → 验收失败

```bash
.venv/bin/python -m pytest -q tests/test_shadow_conflict.py::test_double_shadow_requires_real_delete_not_just_count
```

测试把去重进度伪造成「已完成」、补一行死信，但两枚印记原样留着。`verify/dedup` 必须报不过，
至少这两项失败：**双影清零**、**已处置即无待处置印记**。

### 4.2 缺排他锁 → 双写各留一枚

```bash
.venv/bin/python -m pytest -q tests/test_shadow_conflict.py::test_concurrent_stamp_single_winner
```

两名线程同时盖同一码：一个 200、一个 409「批次已盖印」，库里该码只有 1 枚印记，库存值与它同值。

### 4.3 双影未停

未处置时访问 `POST /api/batches/<双影批次id>/stamp`：409，不会再多留一枚。

---

## 5. 一处写库、失败短句

- 合并只用**一个** `BEGIN IMMEDIATE` 事务：删行 + 写死信 + 改库存 + 写进度 + 记结局同一笔提交；
  任何一步失败整体回滚，接口只回一句短话（如「无双影可处置」「重打水印值需与原印记不同」）。
- 重复点合并：409「无双影可处置」；不存在的运行码：404「运行码不存在」。
- 手动盖印绕不过排队席：409「该运行码待排队席重打」。

---

## 6. 编排任务查合并结局

```bash
curl -s $C $B/api/merge-outcomes | python3 -m json.tool
curl -s $C "$B/api/merge-outcomes?run_code=pay-20261115-01" | python3 -m json.tool
```

按运行码过滤时只回该码的处置历史（含 `strategy / outcome / kept_mark_value / dead_count / status`）。

---

## 7. 旧库迁移（可选二验）

旧版把第二枚藏进 `meta`（key `shadow_wm_<运行码>`）。启动时 `init_db` 会把它搬进印记表、
标成双影待处置，并删掉 meta 那一行；随后与种子双影同样处置。命令见 §3 的「无隐形双影」一项。
