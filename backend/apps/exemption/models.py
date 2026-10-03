"""
异常豁免模型。

核心两张表：

* :class:`Exemption` —— 异常豁免申请/批准单，关联被绕过的规则编码、风险说明、
  有效次数、截止时间以及申请时固化的基础条件快照；
* :class:`ExemptionUse` —— 豁免被目标操作实际消费的记录，双向留存（对豁免单与
  业务操作均使用 PROTECT），因此撤销豁免或删除操作的尝试都无法销毁已发生的
  审计事实。
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

from .rules import rule_choices


class Exemption(models.Model):
    """异常豁免单。"""

    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_REVOKED = 'revoked'
    STATUS_CHOICES = [
        (STATUS_PENDING, '待审批'),
        (STATUS_APPROVED, '已批准'),
        (STATUS_REJECTED, '已拒绝'),
        (STATUS_REVOKED, '已撤销'),
    ]

    number = models.CharField('豁免单号', max_length=40, unique=True)
    operation_type = models.CharField('目标操作类型', max_length=50, default='stock_out')
    # 目标操作范围参数（goods_id、quantity、receiver、receiver_dept 等）
    scope = models.JSONField('目标操作范围', default=dict)

    rule_code = models.CharField('被绕过规则', max_length=50, choices=rule_choices())
    risk_note = models.TextField('风险说明')
    reason = models.TextField('申请事由', blank=True)

    # 申请时该规则的违规事实（证明规则确实会被绕过）
    violation_facts = models.JSONField('规则违规事实', default=dict)
    # 基础条件快照：批准时按当时状态刷新，消费时逐字段复验
    base_condition_snapshot = models.JSONField('基础条件快照', default=dict)

    max_uses = models.PositiveIntegerField('有效次数', default=1)
    used_count = models.PositiveIntegerField('已用次数', default=0)
    expire_at = models.DateTimeField('截止时间')

    status = models.CharField(
        '状态', max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING
    )

    applicant = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='applied_exemptions', verbose_name='申请人'
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='decided_exemptions', verbose_name='审批人'
    )
    decision_remark = models.TextField('审批意见', blank=True)
    decided_at = models.DateTimeField('审批时间', null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='revoked_exemptions', verbose_name='撤销人'
    )
    revoke_reason = models.TextField('撤销原因', blank=True)
    revoked_at = models.DateTimeField('撤销时间', null=True, blank=True)

    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'ex_exemption'
        verbose_name = '异常豁免'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.number} - {self.rule_code}'

    # -- 状态判定 ---------------------------------------------------------
    @property
    def rule(self):
        from .rules import get_rule
        return get_rule(self.rule_code)

    @property
    def rule_name(self):
        rule = self.rule
        return rule.name if rule else self.rule_code

    @property
    def is_one_time(self):
        return self.max_uses == 1

    @property
    def remaining_uses(self):
        return max(0, self.max_uses - self.used_count)

    @property
    def is_expired(self):
        return timezone.now() >= self.expire_at

    @property
    def is_exhausted(self):
        return self.used_count >= self.max_uses

    def is_consumable(self, at=None):
        """是否处于可被消费的状态：已批准、未过期、仍有剩余次数。"""
        expired = (at or timezone.now()) >= self.expire_at
        return (
            self.status == self.STATUS_APPROVED
            and not expired
            and not self.is_exhausted
        )

    @property
    def lifecycle_state(self):
        """供审计展示的终态/派生状态。"""
        if self.status == self.STATUS_APPROVED:
            if self.is_expired:
                return '已过期'
            if self.is_exhausted:
                return '已用尽'
            return '生效中'
        return self.get_status_display()


class ExemptionUse(models.Model):
    """豁免消费记录：一次目标操作对一份豁免的一次消费。"""

    RESULT_CONSUMED = 'consumed'
    RESULT_CHOICES = [
        (RESULT_CONSUMED, '已消费'),
    ]

    exemption = models.ForeignKey(
        Exemption, on_delete=models.PROTECT,
        related_name='uses', verbose_name='豁免单'
    )
    rule_code = models.CharField('规则编码', max_length=50)
    operation_type = models.CharField('目标操作类型', max_length=50)
    # 目标操作的可读引用（如 stock_out#12）
    operation_ref = models.CharField('操作引用', max_length=100)
    stock_out = models.ForeignKey(
        'warehouse.StockOut', on_delete=models.PROTECT, null=True, blank=True,
        related_name='exemption_uses', verbose_name='关联出库记录'
    )

    # 消费时刻的完整留痕
    violation_detail = models.TextField('被绕过规则说明', blank=True)
    violation_facts = models.JSONField('违规事实', default=dict)
    base_condition_at_use = models.JSONField('消费时基础条件', default=dict)
    consumed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='exemption_uses', verbose_name='办理人'
    )
    result = models.CharField('使用结果', max_length=20, choices=RESULT_CHOICES,
                              default=RESULT_CONSUMED)
    consumed_at = models.DateTimeField('消费时间', auto_now_add=True)

    # 一次性豁免的数据库级防重：once=True 的豁免至多存在一条消费记录
    once = models.BooleanField('一次性豁免', default=False)

    class Meta:
        db_table = 'ex_exemption_use'
        verbose_name = '豁免使用记录'
        verbose_name_plural = verbose_name
        ordering = ['-consumed_at']
        constraints = [
            models.UniqueConstraint(
                fields=['exemption'],
                condition=models.Q(once=True),
                name='ex_once_exemption_single_use',
            ),
        ]

    def __str__(self):
        return f'{self.exemption_number} -> {self.operation_ref}'

    @property
    def exemption_number(self):
        return self.exemption_id and self.exemption.number
