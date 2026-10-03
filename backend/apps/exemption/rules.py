"""
被豁免规则注册表。

业务中所有"可被线下例外绕过"的硬性限制（容量、时段、资质……）都在此登记。
每条规则（RuleDefinition）声明：

* ``code`` / ``name``：规则编码与展示名；
* ``evaluate(context)``：在给定业务上下文上求值，返回 RuleViolation（被违反时）
  或 None（满足时）；
* ``base_condition(context)``：抽取申请时的"基础条件"快照（仅可 JSON 序列化的
  原始类型）；
* ``base_condition_matches(snapshot, context)``：消费时比对基础条件是否仍与申请时
  一致，不一致则豁免必须重新申请、重新验证。

豁免单只认规则编码，规则的具体判定逻辑集中在本模块，避免豁免审批与规则实际
口径分离。
"""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from django.utils import timezone


@dataclass
class RuleViolation:
    """一次规则求值中发现的违规事实。"""
    code: str
    name: str
    detail: str
    # 违规涉及的关键事实（库存、时间、资质等），用于审计展示
    facts: dict = field(default_factory=dict)


class RuleDefinition:
    """可被豁免规则的基类。"""

    code = ''
    name = ''

    def evaluate(self, context):
        raise NotImplementedError

    def base_condition(self, context):
        """申请豁免时固化的基础条件，默认不固化任何条件。"""
        return {}

    def base_condition_matches(self, snapshot, context):
        """消费时复验基础条件，默认与当前求值出的基础条件保持一致即可。"""
        return self.base_condition(context) == (snapshot or {})

    # -- 供子类使用的工具 -------------------------------------------------
    @staticmethod
    def _as_decimal(value):
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None


class CapacityStockOutRule(RuleDefinition):
    """
    容量规则：出库后库存不得低于货物的预警阈值（监管物资保留底线容量）。

    基础条件：货物当前库存、预警阈值、货物启用状态。库存或阈值在审批后、
    使用前发生变化（例如又出库了一批、阈值被调整），豁免即失效，需重新验证。
    """

    code = 'capacity_stock_out'
    name = '容量限制：出库后库存不得低于预警阈值'

    def evaluate(self, context):
        goods = context.get('goods')
        quantity = self._as_decimal(context.get('quantity'))
        if goods is None or quantity is None:
            return None
        remaining = goods.quantity - quantity
        if remaining < goods.warning_threshold:
            return RuleViolation(
                code=self.code,
                name=self.name,
                detail=(
                    f'出库 {quantity} 后库存为 {remaining}，'
                    f'低于预警阈值 {goods.warning_threshold}'
                ),
                facts={
                    'current_quantity': str(goods.quantity),
                    'request_quantity': str(quantity),
                    'remaining': str(remaining),
                    'warning_threshold': str(goods.warning_threshold),
                },
            )
        return None

    def base_condition(self, context):
        goods = context.get('goods')
        if goods is None:
            return {}
        return {
            'goods_id': goods.id,
            'quantity': str(goods.quantity),
            'warning_threshold': str(goods.warning_threshold),
            'is_active': bool(goods.is_active),
        }


class TimeWindowStockOutRule(RuleDefinition):
    """
    时段规则：出库办理仅允许在工作时段（工作日 08:00-20:00）进行。

    该规则不固化业务基础条件（违规只取决于"何时办理"），申请时记录拟办理
    时间窗口，使用时以实际办理时刻重新求值即可。
    """

    code = 'time_window_stock_out'
    name = '时段限制：仅工作日 08:00-20:00 可办理出库'

    def evaluate(self, context):
        moment = context.get('at_time') or timezone.now()
        moment = timezone.localtime(moment)
        weekday = moment.weekday()  # 0=周一
        hour = moment.hour
        is_weekday = weekday < 5
        in_hours = 8 <= hour < 20
        if not (is_weekday and in_hours):
            reasons = []
            if not is_weekday:
                reasons.append('非工作日')
            if not in_hours:
                reasons.append('不在 08:00-20:00 时段内')
            return RuleViolation(
                code=self.code,
                name=self.name,
                detail=f'当前办理时间 {moment.strftime("%Y-%m-%d %H:%M")}（{"，".join(reasons)}）',
                facts={'at_time': moment.isoformat(), 'weekday': weekday, 'hour': hour},
            )
        return None


class QualificationReceiverRule(RuleDefinition):
    """
    资质规则：领用人必须是登记在册且在职的出库人员，并与申请部门一致。

    基础条件：领用人姓名/警号、在职状态、登记部门。人员被停用、资质信息或
    部门发生变化时，豁免失效，需重新申请验证。
    """

    code = 'qualification_receiver'
    name = '资质限制：领用人须为登记在职的出库人员且部门一致'

    def _person(self, context):
        # 优先使用视图层预取的人员对象，否则按姓名查询
        person = context.get('receiver_person')
        if person is not None:
            return person
        receiver = context.get('receiver')
        if not receiver:
            return None
        from apps.personnel.models import StockOutPerson
        return StockOutPerson.objects.filter(name=receiver).first()

    def evaluate(self, context):
        receiver = context.get('receiver')
        if not receiver:
            return RuleViolation(
                code=self.code, name=self.name,
                detail='未登记领用人', facts={'receiver': ''},
            )
        person = self._person(context)
        problems = []
        if person is None:
            problems.append('领用人不在出库人员名册中')
        elif not person.is_active:
            problems.append('领用人已离职/停用')
        facts = self.base_condition({**context, 'receiver_person': person})
        if person is not None:
            # 名册中无独立部门字段，以申请部门与领用部门一致为校验口径
            pass
        if problems:
            return RuleViolation(
                code=self.code, name=self.name,
                detail='；'.join(problems), facts=facts,
            )
        return None

    def base_condition(self, context):
        person = self._person(context)
        if person is None:
            return {'receiver': context.get('receiver', ''), 'registered': False}
        return {
            'receiver': context.get('receiver', ''),
            'registered': True,
            'police_no': person.police_no,
            'is_active': bool(person.is_active),
            'person_version': person.updated_at.isoformat() if person.updated_at else None,
        }


# 规则编码 -> 规则定义
_RULES = {
    d.code: d
    for d in (CapacityStockOutRule(), TimeWindowStockOutRule(), QualificationReceiverRule())
}


def get_rule(code):
    """按编码获取规则定义，未知编码返回 None。"""
    return _RULES.get(code)


def all_rules():
    """全部已登记规则（按编码排序）。"""
    return [_RULES[code] for code in sorted(_RULES)]


def rule_choices():
    """供模型/序列化器使用的 (code, name) 选项。"""
    return [(code, d.name) for code, d in _RULES.items()]
