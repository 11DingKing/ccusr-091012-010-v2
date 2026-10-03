"""
异常豁免测试：申请-审批-消费-审计全链路，
覆盖一次性幂等、次数、截止时间、基础条件重验、撤销不追溯等规则。
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.exemptions import rules as rule_engine
from apps.exemptions import services
from apps.exemptions.models import Exemption, ExemptionUsage, GuardRule, ExemptionEvent
from apps.personnel.models import StockOutPerson
from apps.warehouse.models import Category, Goods, StockOut, Unit, Variety
from django.test import TestCase


class ExemptionFixture(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('admin1', 'testpass123', role='admin')
        self.operator = User.objects.create_user('op1', 'testpass123', role='user')
        self.admin_client = APIClient()
        self.admin_client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.admin)}")
        self.op_client = APIClient()
        self.op_client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.operator)}")

        self.unit = Unit.objects.create(name='件', created_by=self.admin)
        self.category = Category.objects.create(name='受控器材', unit=self.unit, created_by=self.admin)
        self.variety = Variety.objects.create(name='记录终端', category=self.category, created_by=self.admin)
        self.goods = Goods.objects.create(
            variety=self.variety, name='执法记录终端', code='DEV-001',
            quantity=Decimal('100'), warning_threshold=Decimal('5'),
        )
        # 已备案在职的合规领用人
        self.qualified_person = StockOutPerson.objects.create(
            police_no='J0001', name='张三', phone='13800000001', is_active=True,
        )
        self.cap_rule = GuardRule.objects.get(code=rule_engine.RULE_CAPACITY)
        self.time_rule = GuardRule.objects.get(code=rule_engine.RULE_TIME_WINDOW)
        self.qual_rule = GuardRule.objects.get(code=rule_engine.RULE_QUALIFICATION)

    # ---------- 辅助 ----------

    def set_window(self, start, end):
        """调整时段规则参数"""
        self.time_rule.params = {'start_hour': start, 'end_hour': end}
        self.time_rule.save()

    def make_stock_out(self, quantity='20', receiver='李四', police_no='J9999'):
        return StockOut.objects.create(
            goods=self.goods, operator=self.operator,
            receiver=receiver, receiver_police_no=police_no,
            quantity=Decimal(quantity), status='pending',
        )

    def approve_stock_out(self, so):
        response = self.admin_client.post(f'/api/stock-out/{so.id}/approve/', {}, format='json')
        self.assertEqual(response.status_code, 200, response.content)

    def execute_stock_out(self, client=None, so_id=None):
        client = client or self.op_client
        return client.post(f'/api/stock-out/{so_id}/execute/', {}, format='json')

    def grant(self, rule, scope, max_times=1, days=1, **kwargs):
        """申请并批准一条豁免"""
        exemption = services.create_application(
            rule=rule, applicant=self.operator,
            target_action=rule.target_action, target_scope=scope,
            reason='业务高峰线下批准', risk_note='高峰时段可能影响台账时效',
            max_times=max_times,
            expire_at=timezone.now() + timedelta(days=days),
            **kwargs,
        )
        return services.approve_application(exemption_id=exemption.id, approver=self.admin)


# ==================== 默认规则播种 ====================

class RuleSeedTest(ExemptionFixture):
    def test_default_rules_seeded(self):
        codes = set(GuardRule.objects.values_list('code', flat=True))
        self.assertIn(rule_engine.RULE_CAPACITY, codes)
        self.assertIn(rule_engine.RULE_TIME_WINDOW, codes)
        self.assertIn(rule_engine.RULE_QUALIFICATION, codes)

    def test_rule_list_api(self):
        resp = self.op_client.get('/api/guard-rules/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()['data']), 3)


# ==================== 无违规时不需要豁免 ====================

class CleanPassTest(ExemptionFixture):
    def test_compliant_outbound_needs_no_exemption(self):
        # 时段放开、数量 5（<=10）、领用人已备案
        self.set_window(0, 24)
        so = self.make_stock_out(quantity='5', receiver='张三', police_no='J0001')
        self.approve_stock_out(so)
        resp = self.execute_stock_out(so_id=so.id)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(ExemptionUsage.objects.count(), 0)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal('95'))


# ==================== 无豁免/未批准即阻断 ====================

class BlockedWithoutExemptionTest(ExemptionFixture):
    def test_after_hours_blocked_without_exemption(self):
        self.set_window(0, 0)  # 任何时刻都不在窗口内
        so = self.make_stock_out(quantity='5', receiver='张三', police_no='J0001')
        self.approve_stock_out(so)
        resp = self.execute_stock_out(so_id=so.id)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['data']['violations'][0]['rule_code'],
                         rule_engine.RULE_TIME_WINDOW)
        # 被阻断：库存未变、无消费记录
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal('100'))
        self.assertEqual(ExemptionUsage.objects.count(), 0)

    def test_pending_exemption_cannot_be_consumed(self):
        self.set_window(0, 0)
        # 只申请、不批准
        services.create_application(
            rule=self.time_rule, applicant=self.operator,
            target_action=self.time_rule.target_action, target_scope={},
            reason='紧急任务', risk_note='低',
            expire_at=timezone.now() + timedelta(days=1),
        )
        so = self.make_stock_out(quantity='5', receiver='张三', police_no='J0001')
        self.approve_stock_out(so)
        resp = self.execute_stock_out(so_id=so.id)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(ExemptionUsage.objects.count(), 0)

    def test_physical_stock_shortfall_never_exemptible(self):
        # 物理库存不足是硬约束，容量豁免也救不了
        self.set_window(0, 24)
        so = self.make_stock_out(quantity='5', receiver='张三', police_no='J0001')
        self.goods.quantity = Decimal('2')
        self.goods.save()
        self.grant(self.cap_rule, {'goods_id': self.goods.id})
        self.approve_stock_out(so)
        resp = self.execute_stock_out(so_id=so.id)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('库存不足', resp.json()['message'])
        self.assertEqual(ExemptionUsage.objects.count(), 0)


# ==================== 批准后消费、一次性不重复 ====================

class OneTimeConsumptionTest(ExemptionFixture):
    def test_one_time_exemption_consumed_once_then_blocked(self):
        self.set_window(0, 0)
        self.grant(self.time_rule, {})

        so1 = self.make_stock_out(quantity='5', receiver='张三', police_no='J0001')
        self.approve_stock_out(so1)
        resp1 = self.execute_stock_out(so_id=so1.id)
        self.assertEqual(resp1.status_code, 200, resp1.content)

        exemption = Exemption.objects.get(rule=self.time_rule)
        self.assertEqual(exemption.used_count, 1)
        self.assertEqual(exemption.remaining_times, 0)
        usage = ExemptionUsage.objects.get()
        self.assertEqual(usage.result, ExemptionUsage.RESULT_SUCCESS)

        # 一次性豁免不得重复使用：第二次出库被阻断
        so2 = self.make_stock_out(quantity='5', receiver='张三', police_no='J0001')
        self.approve_stock_out(so2)
        resp2 = self.execute_stock_out(so_id=so2.id)
        self.assertEqual(resp2.status_code, 400)
        self.assertEqual(ExemptionUsage.objects.count(), 1)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal('95'))  # 只扣减了一次

    def test_multi_use_exemption_respects_max_times(self):
        self.set_window(0, 0)
        self.grant(self.time_rule, {}, max_times=2)

        for _ in range(2):
            so = self.make_stock_out(quantity='5', receiver='张三', police_no='J0001')
            self.approve_stock_out(so)
            resp = self.execute_stock_out(so_id=so.id)
            self.assertEqual(resp.status_code, 200, resp.content)

        # 第 3 次超次数被阻断
        so3 = self.make_stock_out(quantity='5', receiver='张三', police_no='J0001')
        self.approve_stock_out(so3)
        resp = self.execute_stock_out(so_id=so3.id)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(ExemptionUsage.objects.filter(result='success').count(), 2)

    def test_expired_approved_exemption_not_consumable(self):
        self.set_window(0, 0)
        exemption = self.grant(self.time_rule, {})
        # 批准后截止时间被改到过去：消费侧必须拒绝
        exemption.expire_at = timezone.now() - timedelta(hours=1)
        exemption.save()
        so = self.make_stock_out(quantity='5', receiver='张三', police_no='J0001')
        self.approve_stock_out(so)
        resp = self.execute_stock_out(so_id=so.id)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(ExemptionUsage.objects.count(), 0)
        exemption.refresh_from_db()
        self.assertEqual(exemption.status, Exemption.STATUS_EXPIRED)

    def test_approve_rejects_past_expiry(self):
        with self.assertRaises(services.ExemptionError):
            self.grant(self.time_rule, {}, days=-1)


# ==================== 三类规则分别豁免 ====================

class RuleTypeCoverageTest(ExemptionFixture):
    def test_capacity_exemption(self):
        self.set_window(0, 24)
        self.grant(self.cap_rule, {'goods_id': self.goods.id})
        so = self.make_stock_out(quantity='20', receiver='张三', police_no='J0001')
        self.approve_stock_out(so)
        resp = self.execute_stock_out(so_id=so.id)
        self.assertEqual(resp.status_code, 200, resp.content)
        usage = ExemptionUsage.objects.get()
        self.assertEqual(usage.rule_snapshot['code'], rule_engine.RULE_CAPACITY)

    def test_qualification_exemption_for_unregistered_receiver(self):
        self.set_window(0, 24)
        # 李四未备案，资质违规；批准其资质豁免后放行
        self.grant(self.qual_rule, {'receiver': '李四', 'police_no': 'J9999'})
        so = self.make_stock_out(quantity='5', receiver='李四', police_no='J9999')
        self.approve_stock_out(so)
        resp = self.execute_stock_out(so_id=so.id)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(ExemptionUsage.objects.get().rule.code, rule_engine.RULE_QUALIFICATION)

    def test_wrong_scope_exemption_does_not_cover(self):
        self.set_window(0, 24)
        # 容量豁免绑定的是另一个货物，不能覆盖本次货物
        other_goods = Goods.objects.create(
            variety=self.variety, name='其他设备', code='DEV-002', quantity=Decimal('50'),
        )
        self.grant(self.cap_rule, {'goods_id': other_goods.id})
        so = self.make_stock_out(quantity='20', receiver='张三', police_no='J0001')
        self.approve_stock_out(so)
        resp = self.execute_stock_out(so_id=so.id)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(ExemptionUsage.objects.count(), 0)

    def test_multiple_violations_require_all_covered(self):
        # 同时超容量、在禁时、无资质：只批容量豁免，仍被阻断
        self.set_window(0, 0)
        self.grant(self.cap_rule, {'goods_id': self.goods.id})
        so = self.make_stock_out(quantity='20', receiver='李四', police_no='J9999')
        self.approve_stock_out(so)
        resp = self.execute_stock_out(so_id=so.id)
        self.assertEqual(resp.status_code, 400)
        codes = {v['rule_code'] for v in resp.json()['data']['violations']}
        self.assertIn(rule_engine.RULE_TIME_WINDOW, codes)
        self.assertIn(rule_engine.RULE_QUALIFICATION, codes)
        # 整体事务回滚：容量豁免未被占用
        self.assertEqual(ExemptionUsage.objects.count(), 0)


# ==================== 基础条件变化重新验证 ====================

class RevalidationTest(ExemptionFixture):
    def test_basis_change_invalidates_on_consume(self):
        self.set_window(0, 24)
        exemption = self.grant(self.cap_rule, {'goods_id': self.goods.id})

        # 批准后容量上限由 10 调整为 15（本次数量 20 仍超限），基础条件已变化
        self.cap_rule.params = {'max_qty': 15}
        self.cap_rule.save()

        so = self.make_stock_out(quantity='20', receiver='张三', police_no='J0001')
        self.approve_stock_out(so)
        resp = self.execute_stock_out(so_id=so.id)
        self.assertEqual(resp.status_code, 400)
        exemption.refresh_from_db()
        self.assertEqual(exemption.status, Exemption.STATUS_REVALIDATION_FAILED)
        self.assertEqual(ExemptionUsage.objects.count(), 0)

    def test_qualification_status_change_invalidates(self):
        self.set_window(0, 24)
        # 张三批准时为已备案在职，豁免固化该基础条件；随后被停职
        exemption = self.grant(self.qual_rule, {'receiver': '张三', 'police_no': 'J0001'})
        self.qualified_person.is_active = False
        self.qualified_person.save()

        so = self.make_stock_out(quantity='5', receiver='张三', police_no='J0001')
        self.approve_stock_out(so)
        resp = self.execute_stock_out(so_id=so.id)
        self.assertEqual(resp.status_code, 400)
        exemption.refresh_from_db()
        self.assertEqual(exemption.status, Exemption.STATUS_REVALIDATION_FAILED)

    def test_revalidate_endpoint_reports_unchanged(self):
        self.set_window(0, 0)
        exemption = self.grant(self.time_rule, {})
        resp = self.admin_client.post(f'/api/exemptions/{exemption.id}/revalidate/', {}, format='json')
        self.assertEqual(resp.status_code, 200)
        exemption.refresh_from_db()
        self.assertEqual(exemption.status, Exemption.STATUS_APPROVED)


# ==================== 撤销不追溯 ====================

class RevocationTest(ExemptionFixture):
    def test_revoke_blocks_future_but_keeps_history(self):
        self.set_window(0, 0)
        exemption = self.grant(self.time_rule, {})

        so = self.make_stock_out(quantity='5', receiver='张三', police_no='J0001')
        self.approve_stock_out(so)
        self.assertEqual(self.execute_stock_out(so_id=so.id).status_code, 200)

        # 撤销需要原因；撤销后历史出库与消费记录完整保留
        resp = self.admin_client.post(
            f'/api/exemptions/{exemption.id}/revoke/',
            {'reason': '高峰结束，例外关闭'}, format='json',
        )
        self.assertEqual(resp.status_code, 200)
        exemption.refresh_from_db()
        self.assertEqual(exemption.status, Exemption.STATUS_REVOKED)
        self.assertEqual(
            ExemptionUsage.objects.filter(exemption=exemption).count(), 1
        )
        self.assertEqual(
            ExemptionUsage.objects.get(exemption=exemption).result,
            ExemptionUsage.RESULT_SUCCESS,
        )

        # 撤销后不能再用于新操作
        so2 = self.make_stock_out(quantity='5', receiver='张三', police_no='J0001')
        self.approve_stock_out(so2)
        self.assertEqual(self.execute_stock_out(so_id=so2.id).status_code, 400)

    def test_revoke_requires_reason(self):
        exemption = self.grant(self.time_rule, {})
        resp = self.admin_client.post(
            f'/api/exemptions/{exemption.id}/revoke/', {}, format='json'
        )
        self.assertEqual(resp.status_code, 400)

    def test_non_admin_non_applicant_cannot_revoke(self):
        other = User.objects.create_user('op2', 'testpass123', role='user')
        other_client = APIClient()
        other_client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(other)}")
        exemption = self.grant(self.time_rule, {})
        resp = other_client.post(
            f'/api/exemptions/{exemption.id}/revoke/',
            {'reason': '无权操作'}, format='json',
        )
        self.assertEqual(resp.status_code, 403)


# ==================== 失败回退与重试 ====================

class QuotaRefundTest(ExemptionFixture):
    def _violation(self):
        self.set_window(0, 0)
        ctx = rule_engine.build_context(
            rule_engine.TARGET_STOCK_OUT,
            goods=self.goods, quantity=Decimal('5'),
            receiver='张三', receiver_police_no='J0001',
        )
        return rule_engine.evaluate_rule(self.time_rule, ctx)

    def test_failed_usage_refunds_and_rearm_on_retry(self):
        self.set_window(0, 0)
        exemption = self.grant(self.time_rule, {})
        target_ref = {'stock_out_id': 999, 'goods_id': self.goods.id,
                      'receiver': '张三', 'police_no': 'J0001'}
        violation = self._violation()

        usages = services.consume(
            action=rule_engine.TARGET_STOCK_OUT, target_ref=target_ref,
            violations=[violation], consumer=self.operator,
        )
        self.assertEqual(len(usages), 1)
        exemption.refresh_from_db()
        self.assertEqual(exemption.used_count, 1)

        # 目标操作失败：名额回退
        services.confirm_usage(
            target_action=rule_engine.TARGET_STOCK_OUT, target_ref=target_ref,
            consumer=self.operator, success=False, detail='设备故障未出库',
        )
        exemption.refresh_from_db()
        self.assertEqual(exemption.used_count, 0)
        usage = ExemptionUsage.objects.get()
        self.assertEqual(usage.result, ExemptionUsage.RESULT_FAILED)
        self.assertTrue(usage.quota_refunded)

        # 同笔业务重试：复用原记录重新占用，而不是重复消费
        usages2 = services.consume(
            action=rule_engine.TARGET_STOCK_OUT, target_ref=target_ref,
            violations=[self._violation()], consumer=self.operator,
        )
        self.assertEqual(ExemptionUsage.objects.count(), 1)
        self.assertEqual(usages2[0].id, usage.id)
        self.assertEqual(usages2[0].result, ExemptionUsage.RESULT_CONSUMED)
        exemption.refresh_from_db()
        self.assertEqual(exemption.used_count, 1)


# ==================== 审计查询 ====================

class AuditTest(ExemptionFixture):
    def test_audit_shows_rule_basis_and_result(self):
        self.set_window(0, 0)
        exemption = self.grant(self.time_rule, {})
        so = self.make_stock_out(quantity='5', receiver='张三', police_no='J0001')
        self.approve_stock_out(so)
        self.execute_stock_out(so_id=so.id)

        # 出库详情内嵌豁免线索
        detail = self.op_client.get(f'/api/stock-out/{so.id}/')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.json()['data']['exemption_usages']), 1)

        # 审计接口：原规则 + 豁免依据 + 实际使用结果
        resp = self.op_client.get(f'/api/exemption-audit/?stock_out_id={so.id}')
        self.assertEqual(resp.status_code, 200)
        record = resp.json()['data']['list'][0]
        self.assertEqual(record['rule']['code'], rule_engine.RULE_TIME_WINDOW)
        self.assertEqual(record['exemption']['id'], exemption.id)
        self.assertEqual(record['exemption']['risk_note'], '高峰时段可能影响台账时效')
        self.assertEqual(record['exemption']['approver'], self.admin.username)
        self.assertTrue(record['exemption']['basis_snapshot'])
        self.assertIn('allowed_window', record['exemption']['basis_snapshot'])
        self.assertEqual(record['usage']['result'], ExemptionUsage.RESULT_SUCCESS)
        self.assertEqual(record['usage']['consumer'], self.operator.username)
        self.assertEqual(record['violation']['rule_code'], rule_engine.RULE_TIME_WINDOW)
        self.assertEqual(resp.json()['data']['result_summary'].get('success'), 1)

    def test_event_trail_recorded(self):
        self.set_window(0, 0)
        exemption = self.grant(self.time_rule, {})
        types = set(
            ExemptionEvent.objects.filter(exemption=exemption)
            .values_list('type', flat=True)
        )
        self.assertIn('submitted', types)
        self.assertIn('approved', types)

        so = self.make_stock_out(quantity='5', receiver='张三', police_no='J0001')
        self.approve_stock_out(so)
        self.execute_stock_out(so_id=so.id)
        types = set(
            ExemptionEvent.objects.filter(exemption=exemption)
            .values_list('type', flat=True)
        )
        self.assertIn('consumed', types)
        self.assertIn('usage_finished', types)

        resp = self.op_client.get(f'/api/exemptions/{exemption.id}/events/')
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(resp.json()['data']), 4)


# ==================== 申请校验与权限 ====================

class ApplicationAPITest(ExemptionFixture):
    def test_create_application_requires_risk_note(self):
        resp = self.op_client.post('/api/exemptions/', {
            'rule': self.cap_rule.id,
            'target_scope': {'goods_id': self.goods.id},
            'reason': '高峰期',
            'risk_note': '',
            'expire_at': (timezone.now() + timedelta(days=1)).isoformat(),
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_create_and_approve_flow_via_api(self):
        resp = self.op_client.post('/api/exemptions/', {
            'rule': self.time_rule.id,
            'target_scope': {},
            'reason': '夜间紧急取证任务',
            'risk_note': '夜间值班人手少，需双人复核',
            'mitigation': '执行后次日补录台账',
            'max_times': 1,
            'expire_at': (timezone.now() + timedelta(days=2)).isoformat(),
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        exemption_id = resp.json()['data']['id']
        self.assertEqual(resp.json()['data']['status'], 'pending')

        # 普通用户无权批准
        forbidden = self.op_client.post(f'/api/exemptions/{exemption_id}/approve/', {}, format='json')
        self.assertEqual(forbidden.status_code, 403)

        ok = self.admin_client.post(
            f'/api/exemptions/{exemption_id}/approve/',
            {'remark': '同意，注意补录'}, format='json',
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()['data']['status'], 'approved')
        self.assertTrue(ok.json()['data']['basis_fingerprint'])

    def test_capacity_scope_must_reference_existing_goods(self):
        resp = self.op_client.post('/api/exemptions/', {
            'rule': self.cap_rule.id,
            'target_scope': {},
            'reason': '需要超量发放物资',
            'risk_note': '低风险',
            'expire_at': (timezone.now() + timedelta(days=1)).isoformat(),
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('货物', resp.json()['message'])

    def test_guard_check_preview(self):
        self.set_window(0, 24)
        resp = self.op_client.post('/api/guard-check/', {
            'goods': self.goods.id, 'quantity': '20',
            'receiver': '张三', 'receiver_police_no': 'J0001',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertTrue(data['blocked'])
        self.assertEqual(data['violations'][0]['rule_code'], rule_engine.RULE_CAPACITY)

    def test_requires_authentication(self):
        resp = APIClient().get('/api/exemptions/')
        self.assertEqual(resp.status_code, 401)
