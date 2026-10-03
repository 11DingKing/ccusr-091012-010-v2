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

## 异常豁免

业务高峰时，容量、时段、资质等硬性限制可能被线下批准后绕过。系统通过"异常豁免单"将
例外审批前置留痕，杜绝"只看到一次成功操作、看不到为何放行"。

可被绕过的规则在 `apps/exemption/rules.py` 登记，当前包含：

| 规则编码 | 含义 |
| --- | --- |
| `capacity_stock_out` | 容量限制：出库后库存不得低于预警阈值 |
| `time_window_stock_out` | 时段限制：仅工作日 08:00-20:00 可办理出库 |
| `qualification_receiver` | 资质限制：领用人须为登记在职的出库人员 |

流程与约束：

1. `GET /api/rules/` 查询可豁免规则目录；
2. `POST /api/exemptions/` 提交申请，须指明被绕过规则、目标操作范围、**风险说明**、
   **有效次数**与**截止时间**；系统会当场验证规则确实会被违反，并固化基础条件快照；
3. `POST /api/exemptions/{id}/approve|reject/` 审批（申请人不能审批自己的申请，
   仅管理员可审批），**只有已批准的豁免才能被目标操作消费**；
4. `POST /api/stock-out/execute/` 办理出库时逐规则校验：被违反的规则必须由一份
   已批准、未过期、仍有剩余次数、范围匹配且基础条件未变化的豁免覆盖，否则整笔拒绝；
   - 一次性豁免（有效次数=1）数据库级限制只能消费一次；
   - 批准后基础条件（库存/阈值、人员资质状态等）发生变化，豁免失效，须重新申请验证；
5. `POST /api/exemptions/{id}/revoke/` 撤销豁免只对未来生效，**已发生的使用记录
   不会被追溯删除**（消费记录对豁免单与出库记录均为 PROTECT 关联）；
6. 审计查询：`GET /api/exemptions/{id}/` 与 `GET /api/stock-out/{id}/audit/`
   同时展示**原规则、豁免依据（风险说明/审批人/时效/次数/基础条件快照）与实际
   使用结果**。

