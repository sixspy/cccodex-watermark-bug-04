# cc-watermark-lab

CC_Codex 二期（26.11）**数据工程**产品线：**血缘水印台**（BlackSheep + Alpine CDN + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**产出表水印模板 → 批次盖印 → 下游追索 → 缺口对账**

与现有七线正交（不是采集目录、ETL 管道、同步源、解析入库、编排流、种子摄取、留存裁撤）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数据血缘与水印（产出表盖印 / 追索闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、BlackSheep、Uvicorn、SQLite |
| 前端 | 原生 JS SPA（全中文，底栏），雾灰底 + 青柠强调；不依赖外网 CDN |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 产出表水印模板（表名 / 列名 / 现行版本）
2. 向运行批次盖印水印值
3. 按水印值追索下游批次
4. 缺口对账（已盖印 vs 应盖印）

## 双影处置（bug-04）

同一运行码出现两枚印记（双影）时，差额台报警、去重入库停等。冲突目录 / 印记列表 /
批次卡片 / 去重进度四处同时可见，处置只有两种落库命运：

- **留存先手**：后手行整笔回滚送进死信，先手印记转为有效；
- **整笔回滚·待重打**：两枚一并回滚进死信，交排队席重打唯一印记。

`stamp_marks` 是盖印的唯一事实来源，`run_batches.watermark_value` 与唯一有效印记同值同源
（库内部分唯一索引 + 同源触发器兜底）。盖印与合并都走 `BEGIN IMMEDIATE`，两名灌数员并发
同打一码只有一个成功。五格新底栏：冲突 · 印记 · 去重（另有模板 / 批次 / 追索 / 缺口）。

复现与二验步骤见 [`docs/REPRO-shadow-04.md`](docs/REPRO-shadow-04.md)。
验收口：

- `GET /api/verify/dedup` —— 去重一遍九项（双影清零 / 单一有效印记 / 同源 / 同值 / 无隐形双影 /
  死信计数一致 / 已处置即无待处置印记 / 停等清零 / 排队席清空）
- `GET /api/verify/gap` —— 差额一遍四项
- `GET /api/merge-outcomes?run_code=...` —— 编排任务查合并结局

预留切口（未做，供后续功能题）：模板换版生效窗、盖印作废回灌、批量盖印互斥、跨表依赖冻结、追索深度限制等。

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-watermark-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:7000/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **7000**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
