# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

## 运行环境

- Python 3.11
- Django REST Framework
- SQLite

## 安装与初始化

```bash
python -m pip install -r backend/requirements.txt
cd backend
python manage.py migrate --run-syncdb
```

## 测试

```bash
cd backend
pytest -q
```

## 编译检查

```bash
python -m compileall -q backend
```

## API 验收

```bash
cd backend
python manage.py migrate --run-syncdb
python manage.py shell -c "from rest_framework.test import APIClient; from apps.authentication.models import User; u=User.objects.create_user('smoke','safe-pass',role='admin'); c=APIClient(); r=c.post('/api/auth/login/',{'username':'smoke','password':'safe-pass'},format='json'); print(r.status_code, bool(r.json()['data']['token']))"
```

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```

## 异常豁免（规则例外管理）

业务高峰线下批准跳过容量、时段或资质限制时，必须在线上留下可追溯的豁免记录。
`apps.exemptions` 提供完整的申请—审批—消费—审计闭环，出库（`warehouse.stock_out`）
是当前被守护的目标操作。

### 规则与生命周期

- 默认守护规则（迁移自动播种）：
  - `stock_out_inventory_capacity` 单次出库容量限制（默认 ≤10）
  - `stock_out_time_window` 出库时段限制（默认 08:00-20:00）
  - `stock_out_receiver_qualification` 领用人资质备案限制
- 豁免申请须关联**被绕过的规则**、填写**风险说明**、**有效次数**（`max_times`，
  `1` 为一次性，`0` 为有效期内不限次）与**截止时间** `expire_at`。
- 只有 **approved** 的豁免才能被目标操作消费；待审批、已拒绝、已撤销、已过期、
  基础条件变化失效的豁免一律阻断操作。
- 批准时固化基础条件快照与指纹（如货物身份、时段窗口、领用人资质状态）；
  消费时重算指纹，**基础条件变化即豁免失效**，需重新申请验证。
- 一次性豁免不得重复使用：消费在事务内对豁免行加锁，并以
  `(豁免, 业务对象)` 唯一约束双重兜底；目标操作失败时回退名额，
  同一笔业务重试复用原记录，不产生重复消费。
- **撤销只对未来生效**，不追溯删除已发生的出库与使用记录。
- 物理库存不足是硬约束，任何豁免都不能放行。

### 主要接口（均需登录，审批/出库审批限管理员）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/guard-rules/` | 被守护规则列表 |
| POST | `/api/guard-check/` | 操作前试算：触发的规则与可用豁免 |
| POST | `/api/exemptions/` | 创建豁免申请 |
| GET | `/api/exemptions/` | 豁免列表（按状态/规则/类别过滤） |
| POST | `/api/exemptions/{id}/approve/` | 批准（固化基础条件指纹） |
| POST | `/api/exemptions/{id}/reject/` | 拒绝 |
| POST | `/api/exemptions/{id}/revoke/` | 撤销（不追溯历史） |
| POST | `/api/exemptions/{id}/revalidate/` | 基础条件重新验证 |
| GET | `/api/exemptions/{id}/events/` | 生命周期事件轨迹 |
| GET | `/api/exemptions/{id}/usages/` | 该豁免的使用记录 |
| GET | `/api/exemption-audit/` | 审计：原规则+豁免依据+实际使用结果 |
| POST | `/api/stock-out/` | 创建出库申请 |
| POST | `/api/stock-out/{id}/approve/`、`/reject/` | 出库审批 |
| POST | `/api/stock-out/{id}/execute/` | 执行出库（触发规则时消费豁免） |

审计接口（可按 `stock_out_id`、`rule_code`、`category`、`exemption`、
`result`、时间范围过滤）每条记录同时给出：消费时点的**原规则快照**
（含参数）、**违规实况**、**豁免依据快照**（事由、风险说明、批准人、
基础条件快照）以及**实际使用结果**（成功/失败、使用人、名额回退情况）。

