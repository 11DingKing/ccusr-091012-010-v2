"""
出库业务服务：被守护规则覆盖的目标操作。

执行出库时：
1. 物理库存不足属于硬约束，任何豁免都不能放行；
2. 其余规则（容量/时段/资质）逐条求值，先在独立事务中清扫
   已过期/基础条件已变化的豁免（即使操作最终被阻断，失效结论也留痕）；
3. 触发的每一条限制都必须有已批准且可用的豁免，由豁免服务原子消费；
4. 出库落地成功后回填豁免使用结果为 success；
5. 消费名额的回退由 confirm_usage(success=False) 处理。
"""
from django.db import transaction
from django.utils import timezone

from apps.exemptions import rules as rule_engine
from apps.exemptions import services as exemption_services
from apps.exemptions.models import ExemptionUsage
from .models import Goods, StockOut


class StockOutError(Exception):
    """出库业务错误"""


class StockOutBlocked(StockOutError):
    """因规则限制且无有效豁免，出库被阻断"""

    def __init__(self, message, violations=None):
        super().__init__(message)
        self.violations = violations or []


def _build_context(goods, stock_out):
    return rule_engine.build_context(
        rule_engine.TARGET_STOCK_OUT,
        goods=goods,
        quantity=stock_out.quantity,
        receiver=stock_out.receiver,
        receiver_police_no=stock_out.receiver_police_no,
    )


def execute_stock_out(*, stock_out_id, operator):
    """
    执行一笔已审批通过的出库。
    返回 (stock_out, usages_qs)。
    """
    stock_out = StockOut.objects.select_related('goods').get(pk=stock_out_id)
    if stock_out.status != 'approved':
        raise StockOutError('仅已审批通过的出库单可以执行')
    if stock_out.stock_out_time is not None:
        raise StockOutError('该出库单已执行，不能重复出库')

    target_ref = {
        'stock_out_id': stock_out.id,
        'goods_id': stock_out.goods_id,
        'receiver': stock_out.receiver,
        'police_no': stock_out.receiver_police_no,
    }

    # 阶段一：按当前状态试算规则，并在独立事务中提交失效清扫
    violations = rule_engine.evaluate_action(
        rule_engine.TARGET_STOCK_OUT, _build_context(stock_out.goods, stock_out)
    )
    if violations:
        exemption_services.sweep_stale_exemptions(
            action=rule_engine.TARGET_STOCK_OUT,
            target_ref=target_ref,
            violations=violations,
            actor=operator,
        )

    # 阶段二：行锁内复核规则、消费豁免、扣减库存
    with transaction.atomic():
        locked_out = (
            StockOut.objects
            .select_for_update()
            .select_related('goods')
            .get(pk=stock_out_id)
        )
        goods = Goods.objects.select_for_update().get(pk=locked_out.goods_id)

        # 物理库存是硬约束：无库存可发，豁免不允许放行
        if locked_out.quantity > goods.quantity:
            raise StockOutError(
                f'库存不足：当前库存 {goods.quantity}，本次申请出库 {locked_out.quantity}'
            )

        # 锁内复核规则（避免清扫与消费之间状态再变）
        locked_violations = rule_engine.evaluate_action(
            rule_engine.TARGET_STOCK_OUT, _build_context(goods, locked_out)
        )
        try:
            usages = exemption_services.consume(
                action=rule_engine.TARGET_STOCK_OUT,
                target_ref=target_ref,
                violations=locked_violations,
                consumer=operator,
                detail=f'出库单 #{locked_out.id} 执行时消费',
            )
        except exemption_services.ExemptionConsumptionError as exc:
            raise StockOutBlocked(str(exc), violations=exc.violations)

        goods.quantity -= locked_out.quantity
        goods.save(update_fields=['quantity', 'updated_at'])
        locked_out.status = 'completed'
        locked_out.stock_out_time = timezone.now()
        locked_out.save(update_fields=['status', 'stock_out_time'])

        exemption_services.confirm_usage(
            target_action=rule_engine.TARGET_STOCK_OUT,
            target_ref=target_ref,
            consumer=operator,
            success=True,
            detail=f'出库单 #{locked_out.id} 已完成，实际出库 {locked_out.quantity}',
        )

    usages_qs = ExemptionUsage.objects.filter(
        target_action=rule_engine.TARGET_STOCK_OUT,
        target_ref__stock_out_id=stock_out_id,
    )
    return locked_out, usages_qs
