"""
异常豁免管理模型

业务高峰时工作人员可能在线下批准跳过容量、时段或资质限制。
本应用把"例外"本身变成可申请、可批准、可消费、可审计的对象：

- GuardRule：被绕过的规则定义（规则代码、适用操作、阻断性参数）
- Exemption：豁免申请，关联规则、风险说明、有效次数与截止时间，
  只有 approved 状态的豁免才能被目标操作消费
- ExemptionUsage：每一次实际消费记录（一次性豁免只能有一条成功消费）
- ExemptionEvent：申请/批准/拒绝/撤销/重验失效等生命周期事件轨迹
"""
from django.db import models
from apps.authentication.models import User


class GuardRule(models.Model):
    """被守护的业务规则定义"""

    CATEGORY_CAPACITY = 'capacity'
    CATEGORY_TIME_WINDOW = 'time_window'
    CATEGORY_QUALIFICATION = 'qualification'
    CATEGORY_CHOICES = [
        (CATEGORY_CAPACITY, '容量限制'),
        (CATEGORY_TIME_WINDOW, '时段限制'),
        (CATEGORY_QUALIFICATION, '资质限制'),
    ]

    code = models.CharField('规则代码', max_length=50, unique=True)
    name = models.CharField('规则名称', max_length=100)
    category = models.CharField('规则类别', max_length=20, choices=CATEGORY_CHOICES)
    target_action = models.CharField(
        '适用目标操作', max_length=100,
        help_text='该规则守护的操作标识，如 warehouse.stock_out'
    )
    description = models.TextField('规则说明', blank=True)
    # 规则当前生效的阈值/条件，序列化为 JSON（如 {"max_qty": 10}）
    params = models.JSONField('规则参数', default=dict, blank=True)
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'ex_guard_rule'
        verbose_name = '守护规则'
        verbose_name_plural = verbose_name
        ordering = ['category', 'code']

    def __str__(self):
        return f"{self.code}（{self.name}）"

    @property
    def category_display_name(self):
        return dict(self.CATEGORY_CHOICES).get(self.category, self.category)


class Exemption(models.Model):
    """异常豁免申请"""

    STATUS_DRAFT = 'draft'
    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_REVOKED = 'revoked'
    STATUS_EXPIRED = 'expired'
    STATUS_REVALIDATION_FAILED = 'revalidation_failed'
    STATUS_CHOICES = [
        (STATUS_DRAFT, '草稿'),
        (STATUS_PENDING, '待审批'),
        (STATUS_APPROVED, '已批准'),
        (STATUS_REJECTED, '已拒绝'),
        (STATUS_REVOKED, '已撤销'),
        (STATUS_EXPIRED, '已过期'),
        (STATUS_REVALIDATION_FAILED, '基础条件变化失效'),
    ]

    # 目标操作的终态集合，用于判断消费是否真的发生（见 ExemptionUsage.USAGE_*）
    USAGE_ONCE = 1
    USAGE_UNLIMITED = 0  # max_times=0 表示有效期内不限次数

    rule = models.ForeignKey(
        GuardRule, on_delete=models.PROTECT,
        related_name='exemptions', verbose_name='被绕过的规则'
    )
    applicant = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='exemption_applications', verbose_name='申请人'
    )
    approver = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='exemption_approvals', verbose_name='批准人'
    )

    target_action = models.CharField('目标操作', max_length=100)
    # 业务对象范围，如 {"goods_id": 12}；为空表示适用于该操作的全部对象
    target_scope = models.JSONField('目标对象范围', default=dict, blank=True)

    reason = models.TextField('申请事由')
    risk_note = models.TextField('风险说明')
    mitigation = models.TextField('风险控制措施', blank=True)

    status = models.CharField(
        '状态', max_length=30, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    max_times = models.PositiveIntegerField(
        '有效次数', default=USAGE_ONCE,
        help_text='允许消费的总次数；0 表示截止时间内不限次数'
    )
    used_count = models.PositiveIntegerField('已用次数', default=0)
    expire_at = models.DateTimeField('截止时间')

    # 批准时固化的基础条件快照与指纹；基础条件变化后需要重新验证
    basis_fingerprint = models.CharField('基础条件指纹', max_length=64, blank=True, default='')
    basis_snapshot = models.JSONField('基础条件快照', default=dict, blank=True)

    approval_remark = models.TextField('审批意见', blank=True)
    approved_at = models.DateTimeField('批准时间', null=True, blank=True)
    revalidated_at = models.DateTimeField('最近基础条件校验时间', null=True, blank=True)

    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'ex_exemption'
        verbose_name = '异常豁免'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'target_action']),
            models.Index(fields=['rule', 'status']),
        ]

    def __str__(self):
        return f"豁免#{self.pk} {self.rule.code} {self.get_status_display()}"

    @property
    def is_one_time(self):
        """一次性豁免"""
        return self.max_times == self.USAGE_ONCE

    @property
    def remaining_times(self):
        """剩余可用次数；不限次数返回 None"""
        if self.max_times == self.USAGE_UNLIMITED:
            return None
        remaining = self.max_times - self.used_count
        return remaining if remaining > 0 else 0

    @property
    def is_expired(self):
        from django.utils import timezone
        return self.expire_at is not None and timezone.now() >= self.expire_at

    def is_usable(self):
        """
        是否可被目标操作消费。只有已批准、未过期、仍有剩余次数的豁免才可用。
        过期会在此处落库为 expired 状态。
        """
        if self.status != self.STATUS_APPROVED:
            return False
        if self.is_expired:
            if self.status == self.STATUS_APPROVED:
                self.status = self.STATUS_EXPIRED
                self.save(update_fields=['status', 'updated_at'])
            return False
        if self.max_times != self.USAGE_UNLIMITED and self.used_count >= self.max_times:
            return False
        return True

    def covers_scope(self, target_ref):
        """判断豁免的目标对象范围是否覆盖本次操作"""
        if not self.target_scope:
            return True
        return all(target_ref.get(key) == value for key, value in self.target_scope.items())


class ExemptionUsage(models.Model):
    """豁免消费记录：豁免被哪一次目标操作、以什么结果使用"""

    RESULT_CONSUMED = 'consumed'     # 已占用名额（目标操作进行中）
    RESULT_SUCCESS = 'success'       # 目标操作实际成功
    RESULT_FAILED = 'failed'         # 目标操作最终失败，名额已回退
    RESULT_CHOICES = [
        (RESULT_CONSUMED, '已占用'),
        (RESULT_SUCCESS, '使用成功'),
        (RESULT_FAILED, '使用失败'),
    ]

    exemption = models.ForeignKey(
        Exemption, on_delete=models.PROTECT,
        related_name='usages', verbose_name='豁免'
    )
    rule = models.ForeignKey(
        GuardRule, on_delete=models.PROTECT,
        related_name='usages', verbose_name='被绕过的规则'
    )
    target_action = models.CharField('目标操作', max_length=100)
    # 业务对象定位，如 {"stock_out_id": 88}
    target_ref = models.JSONField('目标操作定位', default=dict, blank=True)
    # 业务对象唯一键（target_ref 的确定性字符串），用于数据库级幂等：
    # 同一豁免对同一业务对象无论一次性还是多次都只能消费一次
    target_ref_key = models.CharField('目标对象键', max_length=200, default='')

    consumer = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='exemption_usages', verbose_name='实际使用人'
    )
    result = models.CharField('使用结果', max_length=20, choices=RESULT_CHOICES, default=RESULT_CONSUMED)
    detail = models.TextField('使用说明', blank=True)
    quota_refunded = models.BooleanField('名额是否已回退', default=False)

    # 消费时点的原规则与豁免依据快照，规则/豁免随后变更不影响审计还原
    rule_snapshot = models.JSONField('原规则快照', default=dict, blank=True)
    exemption_snapshot = models.JSONField('豁免依据快照', default=dict, blank=True)
    violation_snapshot = models.JSONField('违规实况快照', default=dict, blank=True)

    consumed_at = models.DateTimeField('消费时间', auto_now_add=True)
    finished_at = models.DateTimeField('结果确认时间', null=True, blank=True)

    class Meta:
        db_table = 'ex_exemption_usage'
        verbose_name = '豁免使用记录'
        verbose_name_plural = verbose_name
        ordering = ['-consumed_at']
        indexes = [
            models.Index(fields=['target_action']),
        ]
        constraints = [
            # 同一豁免对同一业务对象只能消费一次，杜绝一次性豁免重复使用，
            # 也保证同一笔业务失败重试时不会重复占用名额
            models.UniqueConstraint(
                fields=['exemption', 'target_ref_key'],
                name='ex_usage_exemption_target_uniq',
            ),
        ]

    def __str__(self):
        return f"{self.exemption_id} -> {self.target_action} {self.get_result_display()}"


class ExemptionEvent(models.Model):
    """豁免生命周期事件（申请/提交/批准/拒绝/撤销/消费/失效），审计轨迹"""

    TYPE_CHOICES = [
        ('created', '创建'),
        ('submitted', '提交审批'),
        ('approved', '批准'),
        ('rejected', '拒绝'),
        ('revoked', '撤销'),
        ('consumed', '消费'),
        ('usage_finished', '使用结果确认'),
        ('revalidated', '基础条件重新校验'),
        ('expired', '过期'),
    ]

    exemption = models.ForeignKey(
        Exemption, on_delete=models.CASCADE,
        related_name='events', verbose_name='豁免'
    )
    type = models.CharField('事件类型', max_length=30, choices=TYPE_CHOICES)
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='exemption_events', verbose_name='操作人'
    )
    detail = models.TextField('事件说明', blank=True)
    payload = models.JSONField('事件数据', default=dict, blank=True)
    created_at = models.DateTimeField('发生时间', auto_now_add=True)

    class Meta:
        db_table = 'ex_exemption_event'
        verbose_name = '豁免事件'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"豁免#{self.exemption_id} {self.get_type_display()}"


def log_event(exemption, event_type, actor=None, detail='', payload=None, using=None):
    """记录一条豁免生命周期事件。"""
    return ExemptionEvent.objects.using(using).create(
        exemption=exemption,
        type=event_type,
        actor=actor,
        detail=detail,
        payload=payload or {},
    )


# 防止一次性豁免并发重复消费的数据库级保障：
# 消费在事务内对豁免行加锁（select_for_update），并以
# ExemptionUsage 中业务对象唯一键 + used_count 校验双重兜底。
__all__ = ['GuardRule', 'Exemption', 'ExemptionUsage', 'ExemptionEvent', 'log_event']
