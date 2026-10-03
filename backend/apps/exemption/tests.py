"""
异常豁免功能测试。

覆盖：规则求值、申请校验、审批职责分离、仅批准可消费、一次性防重、
次数/时效/范围/基础条件复验、撤销不追溯、审计三段展示与目标操作端到端接口。
"""
from datetime import datetime, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.personnel.models import StockOutPerson
from apps.warehouse.models import Goods, StockOut
from .models import Exemption, ExemptionUse
from .rules import (
    CapacityStockOutRule, TimeWindowStockOutRule, QualificationReceiverRule,
    all_rules,
)
from .services import (
    ExemptionError, apply_for_exemption, approve_exemption, reject_exemption,
    revoke_exemption, execute_stock_out, audit_for_stock_out,
)


SATURDAY_NIGHT = timezone.make_aware(datetime(2026, 10, 3, 22, 0))   # 周六 22:00
MONDAY_MORNING = timezone.make_aware(datetime(2026, 10, 5, 10, 0))    # 周一 10:00


class ExemptionFixture(TestCase):
    def setUp(self):
        self.applicant = User.objects.create_user('handler', 'pass123456', role='user')
        self.approver = User.objects.create_user('chief', 'pass123456', role='admin')
        self.other_admin = User.objects.create_user('chief2', 'pass123456', role='admin')

        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {generate_token(self.applicant)}')
        self.admin_client = APIClient()
        self.admin_client.credentials(HTTP_AUTHORIZATION=f'Bearer {generate_token(self.approver)}')

        from apps.warehouse.models import Unit, Category, Variety
        unit = Unit.objects.create(name='件', created_by=self.approver)
        category = Category.objects.create(name='受控器材', unit=unit, created_by=self.approver)
        variety = Variety.objects.create(name='记录终端', category=category, created_by=self.approver)
        self.goods = Goods.objects.create(
            variety=variety, name='执法记录终端', code='DEV-001',
            quantity=Decimal('12'), warning_threshold=Decimal('5'),
        )
        self.person = StockOutPerson.objects.create(
            police_no='1001', name='张三', phone='13800000001', is_active=True,
        )

    # -- 测试辅助 ---------------------------------------------------------
    def scope(self, quantity='10', receiver='张三'):
        return {
            'goods_id': self.goods.id, 'quantity': quantity,
            'receiver': receiver, 'receiver_dept': '保管科',
        }

    def apply(self, rule_code='capacity_stock_out', at_time=MONDAY_MORNING,
              scope=None, max_uses=1, risk_note='高峰保供，库存可临时低于阈值',
              expire_at=None):
        return apply_for_exemption(
            applicant=self.applicant,
            rule_code=rule_code,
            scope=scope or self.scope(),
            risk_note=risk_note,
            reason='业务高峰',
            max_uses=max_uses,
            expire_at=expire_at or timezone.now() + timedelta(days=1),
            at_time=at_time,
        )

    def approved(self, exemption, approver=None):
        return approve_exemption(exemption, approver or self.approver, '情况属实')

    def execute(self, quantity='10', receiver='张三', at_time=MONDAY_MORNING,
                exemption_ids=None):
        return execute_stock_out(
            operator=self.applicant, goods_id=self.goods.id,
            quantity=quantity, receiver=receiver, receiver_dept='保管科',
            exemption_ids=exemption_ids, at_time=at_time,
        )


class RuleRegistryTest(ExemptionFixture):
    def test_three_rules_registered(self):
        codes = {d.code for d in all_rules()}
        self.assertEqual(
            codes,
            {'capacity_stock_out', 'time_window_stock_out', 'qualification_receiver'},
        )

    def test_capacity_rule_violates_below_threshold(self):
        violation = CapacityStockOutRule().evaluate(
            {'goods': self.goods, 'quantity': Decimal('10')}
        )
        self.assertIsNotNone(violation)
        self.assertEqual(violation.facts['remaining'], '2')

    def test_capacity_rule_passes_above_threshold(self):
        self.assertIsNone(
            CapacityStockOutRule().evaluate({'goods': self.goods, 'quantity': Decimal('3')})
        )

    def test_time_window_rule(self):
        self.assertIsNotNone(
            TimeWindowStockOutRule().evaluate({'at_time': SATURDAY_NIGHT})
        )
        self.assertIsNone(
            TimeWindowStockOutRule().evaluate({'at_time': MONDAY_MORNING})
        )

    def test_qualification_rule(self):
        ctx = {'receiver': '张三', 'receiver_person': self.person}
        self.assertIsNone(QualificationReceiverRule().evaluate(ctx))

        self.person.is_active = False
        self.person.save()
        violation = QualificationReceiverRule().evaluate(ctx)
        self.assertIsNotNone(violation)
        self.assertIn('停用', violation.detail)

        self.assertIsNotNone(
            QualificationReceiverRule().evaluate({'receiver': '名册外的人'})
        )


class ApplicationTest(ExemptionFixture):
    def test_apply_requires_real_violation(self):
        # 周一白天、出库 3 件：不违反任何规则，不能申请
        with self.assertRaises(ExemptionError) as ctx:
            self.apply(scope=self.scope(quantity='3'))
        self.assertIn('无需申请豁免', str(ctx.exception))

    def test_apply_requires_risk_note_and_future_expiry(self):
        with self.assertRaises(ExemptionError):
            apply_for_exemption(
                applicant=self.applicant, rule_code='capacity_stock_out',
                scope=self.scope(), risk_note='',
                expire_at=timezone.now() + timedelta(days=1),
                at_time=MONDAY_MORNING,
            )
        with self.assertRaises(ExemptionError):
            self.apply(expire_at=timezone.now() - timedelta(hours=1))

    def test_apply_unknown_rule(self):
        with self.assertRaises(ExemptionError):
            self.apply(rule_code='not_a_rule')

    def test_apply_snapshots_base_condition_and_facts(self):
        exemption = self.apply()
        self.assertEqual(exemption.status, Exemption.STATUS_PENDING)
        self.assertEqual(exemption.base_condition_snapshot['quantity'], '12.00')
        self.assertEqual(exemption.base_condition_snapshot['warning_threshold'], '5.00')
        self.assertEqual(exemption.violation_facts['remaining'], '2.00')
        self.assertTrue(exemption.is_one_time)


class DecisionTest(ExemptionFixture):
    def test_only_approved_exemption_can_be_consumed(self):
        pending = self.apply()
        with self.assertRaises(ExemptionError) as ctx:
            self.execute(exemption_ids=[pending.id])
        self.assertIn('待审批', str(ctx.exception))

        reject_exemption(pending, self.approver)
        with self.assertRaises(ExemptionError) as ctx:
            self.execute(exemption_ids=[pending.id])
        self.assertIn('已拒绝', str(ctx.exception))
        # 库存未被改动
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal('12'))

    def test_separation_of_duties(self):
        exemption = self.apply()
        # 申请人即便具备管理员角色也不能批准自己的申请
        admin_applicant = User.objects.create_user('adminhandler', 'pass123456', role='admin')
        exemption.applicant = admin_applicant
        exemption.save(update_fields=['applicant'])
        with self.assertRaises(ExemptionError):
            approve_exemption(exemption, admin_applicant)
        # 普通用户不能审批
        with self.assertRaises(ExemptionError):
            approve_exemption(exemption, self.applicant)

    def test_double_decision_rejected(self):
        exemption = self.approved(self.apply())
        with self.assertRaises(ExemptionError):
            reject_exemption(exemption, self.other_admin)


class ConsumptionTest(ExemptionFixture):
    def test_approved_one_time_consumption_happy_path(self):
        exemption = self.approved(self.apply())
        stock_out, violations, uses = self.execute(exemption_ids=[exemption.id])

        self.assertEqual(len(violations), 1)
        self.assertEqual(len(uses), 1)
        self.assertEqual(stock_out.status, 'completed')
        exemption.refresh_from_db()
        self.assertEqual(exemption.used_count, 1)
        self.assertEqual(exemption.remaining_uses, 0)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal('2'))

    def test_one_time_exemption_cannot_be_reused(self):
        exemption = self.approved(self.apply())
        self.execute(exemption_ids=[exemption.id])
        # 库存恢复到再次违规的状态，豁免不得二次使用
        self.goods.quantity = Decimal('12')
        self.goods.save()
        with self.assertRaises(ExemptionError) as ctx:
            self.execute(exemption_ids=[exemption.id])
        self.assertIn('已用尽', str(ctx.exception))

    def test_db_level_once_constraint_blocks_duplicate_use(self):
        """并发/绕过服务层时，数据库唯一约束仍阻止一次性豁免重复落账。"""
        exemption = self.approved(self.apply())
        stock_out = StockOut.objects.create(
            goods=self.goods, operator=self.applicant, receiver='张三',
            quantity=Decimal('10'), status='completed',
        )
        ExemptionUse.objects.create(
            exemption=exemption, rule_code='capacity_stock_out',
            operation_type='stock_out', operation_ref=f'stock_out#{stock_out.id}',
            stock_out=stock_out, once=True,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ExemptionUse.objects.create(
                    exemption=exemption, rule_code='capacity_stock_out',
                    operation_type='stock_out', operation_ref=f'stock_out#{stock_out.id}',
                    stock_out=stock_out, once=True,
                )

    def test_multi_use_counts_down(self):
        exemption = self.approved(self.apply(max_uses=2))
        self.execute(exemption_ids=[exemption.id])
        self.goods.quantity = Decimal('12')
        self.goods.save()
        stock_out, _, uses = self.execute(exemption_ids=[exemption.id])
        self.assertEqual(len(uses), 1)
        exemption.refresh_from_db()
        self.assertEqual(exemption.used_count, 2)
        # 第三次
        self.goods.quantity = Decimal('12')
        self.goods.save()
        with self.assertRaises(ExemptionError):
            self.execute(exemption_ids=[exemption.id])

    def test_expired_exemption_unusable(self):
        exemption = self.approved(self.apply())
        Exemption.objects.filter(pk=exemption.pk).update(
            expire_at=timezone.now() - timedelta(minutes=1)
        )
        with self.assertRaises(ExemptionError) as ctx:
            self.execute(exemption_ids=[exemption.id])
        self.assertIn('过期', str(ctx.exception))

    def test_scope_mismatch_quantity_exceeds_declared(self):
        # 申报 10 件，实际办理 11 件：超出授信范围
        exemption = self.approved(self.apply(scope=self.scope(quantity='10')))
        with self.assertRaises(ExemptionError) as ctx:
            self.execute(quantity='11', exemption_ids=[exemption.id])
        self.assertIn('范围不一致', str(ctx.exception))

    def test_base_condition_change_requires_revalidation(self):
        # 批准后预警阈值由 5 调整为 6：基础条件变化，豁免必须重新验证
        exemption = self.approved(self.apply())
        self.goods.warning_threshold = Decimal('6')
        self.goods.save()
        with self.assertRaises(ExemptionError) as ctx:
            self.execute(exemption_ids=[exemption.id])
        self.assertIn('基础条件', str(ctx.exception))
        self.assertIn('重新申请验证', str(ctx.exception))

    def test_qualification_base_condition_change_on_profile_edit(self):
        # 申请时领用人已停用（确实违反资质规则），批准后其档案被编辑（updated_at 变化）
        self.person.is_active = False
        self.person.save()
        exemption = self.approved(
            self.apply(
                rule_code='qualification_receiver',
                scope=self.scope(quantity='3'),
                at_time=MONDAY_MORNING,
            )
        )
        # 档案变更（仍处于停用状态，规则依旧被违反）→ 基础条件已变化
        self.person.phone = '13900000009'
        self.person.save()
        with self.assertRaises(ExemptionError) as ctx:
            self.execute(quantity='3', exemption_ids=[exemption.id])
        self.assertIn('基础条件', str(ctx.exception))

    def test_time_window_exemption_only_off_hours(self):
        exemption = self.approved(
            self.apply(
                rule_code='time_window_stock_out',
                scope=self.scope(quantity='3'),
                at_time=SATURDAY_NIGHT,
            )
        )
        # 周末夜间：消费豁免办理成功
        self.execute(quantity='3', at_time=SATURDAY_NIGHT, exemption_ids=[exemption.id])
        exemption.refresh_from_db()
        self.assertEqual(exemption.used_count, 1)

    def test_operation_without_violation_does_not_consume(self):
        exemption = self.approved(
            self.apply(
                rule_code='time_window_stock_out',
                scope=self.scope(quantity='3'),
                at_time=SATURDAY_NIGHT,
            )
        )
        # 周一白天办理：规则已满足，操作正常放行，不消耗豁免次数
        _, _, uses = self.execute(quantity='3', at_time=MONDAY_MORNING,
                                  exemption_ids=[exemption.id])
        self.assertEqual(uses, [])
        exemption.refresh_from_db()
        self.assertEqual(exemption.used_count, 0)

    def test_missing_exemption_for_violation_rejects_whole_operation(self):
        with self.assertRaises(ExemptionError) as ctx:
            self.execute(exemption_ids=[])
        self.assertIn('容量限制', str(ctx.exception))
        self.assertEqual(StockOut.objects.count(), 0)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal('12'))


class RevocationTest(ExemptionFixture):
    def test_revoked_exemption_blocks_future_use(self):
        exemption = self.approved(self.apply())
        revoke_exemption(exemption, self.other_admin, reason='情况解除')
        with self.assertRaises(ExemptionError) as ctx:
            self.execute(exemption_ids=[exemption.id])
        self.assertIn('已撤销', str(ctx.exception))

    def test_revocation_does_not_retroactively_delete_usage(self):
        exemption = self.approved(self.apply())
        stock_out, _, uses = self.execute(exemption_ids=[exemption.id])
        use_id = uses[0].id

        revoke_exemption(exemption, self.approver, reason='事后撤销')

        # 消费记录仍在，且仍指向已撤销豁免
        use = ExemptionUse.objects.get(pk=use_id)
        self.assertEqual(use.exemption.status, Exemption.STATUS_REVOKED)
        self.assertEqual(use.stock_out_id, stock_out.id)

        # PROTECT：无法通过删除出库记录来抹掉已发生的豁免使用事实
        with self.assertRaises(IntegrityError):
            stock_out.delete()
        self.assertTrue(StockOut.objects.filter(pk=stock_out.id).exists())

        # 审计仍完整呈现
        audit = audit_for_stock_out(StockOut.objects.get(pk=stock_out.id))
        self.assertTrue(audit['has_exemption'])
        item = audit['bypassed_rules'][0]
        self.assertEqual(item['rule_code'], 'capacity_stock_out')
        self.assertEqual(item['exemption']['status'], 'revoked')
        self.assertEqual(item['exemption']['risk_note'],
                         '高峰保供，库存可临时低于阈值')
        self.assertEqual(item['use']['id'], use_id)
        self.assertEqual(item['use']['result'], 'consumed')


class AuditContentTest(ExemptionFixture):
    def test_audit_shows_rule_basis_and_result(self):
        exemption = self.approved(self.apply())
        stock_out, _, _ = self.execute(exemption_ids=[exemption.id])
        audit = audit_for_stock_out(stock_out)

        self.assertEqual(audit['stock_out']['goods_name'], '执法记录终端')
        item = audit['bypassed_rules'][0]
        # 原规则
        self.assertEqual(item['rule_name'], '容量限制：出库后库存不得低于预警阈值')
        self.assertEqual(item['violation_facts']['remaining'], '2.00')
        # 豁免依据
        self.assertEqual(item['exemption']['number'], exemption.number)
        self.assertEqual(item['exemption']['decided_by_name'], 'chief')
        self.assertEqual(item['exemption']['base_condition_snapshot']['quantity'], '12.00')
        # 使用结果
        self.assertEqual(item['use']['operation_ref'], f'stock_out#{stock_out.id}')
        self.assertEqual(item['use']['consumed_by_name'], 'handler')


class ExemptionAPITest(ExemptionFixture):
    def _approved_capacity_exemption(self):
        ex = self.apply()
        return approve_exemption(ex, self.approver)

    def test_rules_catalog(self):
        response = self.client.get('/api/rules/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['data']), 3)

    def test_apply_approve_flow_via_api(self):
        resp = self.client.post('/api/exemptions/', {
            'rule_code': 'capacity_stock_out',
            'goods_id': self.goods.id,
            'quantity': '10',
            'receiver': '张三',
            'risk_note': '高峰保供，库存可临时低于阈值',
            'expire_at': (timezone.now() + timedelta(days=1)).isoformat(),
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        ex_id = resp.json()['data']['id'] if 'id' in resp.json()['data'] else None
        # 审计字段：原规则 + 依据 + 使用结果
        self.assertIn('rule_name', resp.json()['data'])
        self.assertEqual(resp.json()['data']['uses'], [])

        # 申请人自己（普通用户）无权审批
        forbidden = self.client.post(f'/api/exemptions/{ex_id}/approve/', {}, format='json')
        self.assertEqual(forbidden.status_code, 403)

        ok = self.admin_client.post(f'/api/exemptions/{ex_id}/approve/',
                                    {'remark': '同意'}, format='json')
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()['data']['status'], 'approved')

    def test_execute_endpoint_rejects_without_exemption(self):
        resp = self.client.post('/api/stock-out/execute/', {
            'goods_id': self.goods.id, 'quantity': '10', 'receiver': '张三',
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('容量限制', resp.json()['message'])

    def test_execute_endpoint_and_audit_end_to_end(self):
        cap = self._approved_capacity_exemption()
        ids = [cap.id]

        # 若测试运行在非工作时段，同时申请时段豁免，保证端到端确定性
        if TimeWindowStockOutRule().evaluate({'at_time': timezone.now()}) is not None:
            time_ex = apply_for_exemption(
                applicant=self.applicant,
                rule_code='time_window_stock_out',
                scope=self.scope(quantity='10'),
                risk_note='夜间紧急领用',
                expire_at=timezone.now() + timedelta(days=1),
            )
            approve_exemption(time_ex, self.approver)
            ids.append(time_ex.id)

        resp = self.client.post('/api/stock-out/execute/', {
            'goods_id': self.goods.id, 'quantity': '10',
            'receiver': '张三', 'exemption_ids': ids,
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()['data']
        self.assertTrue(data['has_exemption'])
        self.assertIn('capacity_stock_out',
                      [i['rule_code'] for i in data['bypassed_rules']])
        stock_out_id = data['stock_out']['id']

        audit = self.client.get(f'/api/stock-out/{stock_out_id}/audit/')
        self.assertEqual(audit.status_code, 200)
        audited = {
            i['rule_code']: i for i in audit.json()['data']['bypassed_rules']
        }
        self.assertEqual(
            audited['capacity_stock_out']['exemption']['risk_note'],
            '高峰保供，库存可临时低于阈值',
        )

    def test_revoke_endpoint_preserves_history(self):
        cap = self._approved_capacity_exemption()
        # 在确定的工作时段通过服务层先消费一次
        self.execute(exemption_ids=[cap.id])

        revoked = self.admin_client.post(
            f'/api/exemptions/{cap.id}/revoke/', {'reason': '演练结束'}, format='json'
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(revoked.json()['data']['status'], 'revoked')
        # 使用记录仍在，撤销不追溯删除
        self.assertEqual(len(revoked.json()['data']['uses']), 1)

    def test_list_filter_and_anonymous_denied(self):
        self.approved(self.apply())
        resp = self.client.get('/api/exemptions/?status=approved')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['total'], 1)

        anon = APIClient().get('/api/exemptions/')
        self.assertEqual(anon.status_code, 401)

    def test_execute_unknown_goods_404(self):
        resp = self.client.post('/api/stock-out/execute/', {
            'goods_id': 99999, 'quantity': '1', 'receiver': '张三',
        }, format='json')
        self.assertEqual(resp.status_code, 404)
