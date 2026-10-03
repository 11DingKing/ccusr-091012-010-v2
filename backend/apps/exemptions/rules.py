"""
守护规则引擎

以可注册的方式定义"目标操作"必须满足的规则（容量、时段、资质）。
每条规则提供两个能力：

1. evaluate(context, params)：对一次具体操作求值，返回违规描述；
2. build_basis(scope, params)：生成豁免批准时固化的基础条件快照与
   指纹材料，操作消费豁免时重新计算并比对——基础条件变化即判定失效。

新增规则：实现 handler 与 basis_builder，注册到 REGISTRY，
并在 DEFAULT_RULE_SPECS 中补充种子定义（数据迁移会写入 GuardRule 表）。
"""
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime

from django.utils import timezone

TARGET_STOCK_OUT = 'warehouse.stock_out'

RULE_CAPACITY = 'stock_out_inventory_capacity'
RULE_TIME_WINDOW = 'stock_out_time_window'
RULE_QUALIFICATION = 'stock_out_receiver_qualification'


@dataclass
class StockOutContext:
    """出库操作上下文"""
    goods: object = None
    quantity: object = None
    receiver: str = ''
    receiver_police_no: str = ''
    at_time: datetime = None

    def __post_init__(self):
        if self.at_time is None:
            self.at_time = timezone.now()


@dataclass
class Violation:
    """规则求值结果：一次违规"""
    rule_id: int
    rule_code: str
    rule_name: str
    category: str
    message: str
    actual: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            'rule_id': self.rule_id,
            'rule_code': self.rule_code,
            'rule_name': self.rule_name,
            'category': self.category,
            'category_display': dict(self._category_choices()).get(self.category, self.category),
            'message': self.message,
            'actual': self.actual,
        }

    @staticmethod
    def _category_choices():
        from .models import GuardRule
        return GuardRule.CATEGORY_CHOICES


@dataclass
class Basis:
    """
    基础条件快照。

    material：参与指纹计算的关键条件，变化即视为基础条件变化；
    info：仅用于审计展示的辅助信息，不参与指纹。
    """
    material: dict
    info: dict = field(default_factory=dict)

    @property
    def snapshot(self):
        data = dict(self.material)
        data['_info'] = self.info
        return data


def canonical_json(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)


def fingerprint_of(material):
    return hashlib.sha256(canonical_json(material).encode('utf-8')).hexdigest()


# ==================== 容量限制 ====================

def _validate_capacity_scope(scope):
    goods_id = scope.get('goods_id')
    if not goods_id:
        raise ValueError('容量豁免必须指定货物（goods_id）')
    from apps.warehouse.models import Goods
    goods = Goods.objects.filter(pk=goods_id, is_active=True).first()
    if not goods:
        raise ValueError('指定的货物不存在或已停用')
    return goods


def evaluate_capacity(ctx, params):
    """单次出库数量不得超过规定上限。"""
    max_qty = float(params.get('max_qty', 10))
    if ctx.quantity is None:
        return None
    if ctx.quantity > max_qty:
        return (
            f'单次出库数量 {ctx.quantity} 超过常规上限 {max_qty:g}',
            {'quantity': float(ctx.quantity), 'max_qty_per_outbound': max_qty},
        )
    return None


def basis_capacity(scope, params):
    goods = _validate_capacity_scope(scope)
    return Basis(
        # 关键条件：货物身份/启用状态 + 申请时的容量上限；
        # 任一变化（货物停用、上限调整）都要求重新验证
        material={
            'goods_id': goods.id,
            'goods_code': goods.code,
            'goods_is_active': goods.is_active,
            'max_qty_per_outbound': float(params.get('max_qty', 10)),
        },
        info={'goods_name': goods.name},
    )


# ==================== 时段限制 ====================

def evaluate_time_window(ctx, params):
    """仅允许在每日允许时段内出库。"""
    start_hour = int(params.get('start_hour', 8))
    end_hour = int(params.get('end_hour', 20))
    local_time = timezone.localtime(ctx.at_time)
    hour = local_time.hour + local_time.minute / 60
    weekdays = params.get('weekdays')
    weekday_ok = weekdays is None or local_time.weekday() in [int(d) for d in weekdays]
    if not (start_hour <= hour < end_hour) or not weekday_ok:
        return (
            f'当前时间 {local_time:%Y-%m-%d %H:%M} 不在允许出库时段 '
            f'{start_hour:02d}:00-{end_hour:02d}:00 内',
            {
                'at_time': local_time.strftime('%Y-%m-%d %H:%M'),
                'allowed_window': [start_hour, end_hour],
                'weekday': local_time.weekday(),
            },
        )
    return None


def basis_time_window(scope, params):
    start_hour = int(params.get('start_hour', 8))
    end_hour = int(params.get('end_hour', 20))
    weekdays = params.get('weekdays')
    return Basis(
        material={
            'allowed_window': [start_hour, end_hour],
            'weekdays': sorted(int(d) for d in weekdays) if weekdays is not None else None,
        },
    )


# ==================== 资质限制 ====================

def _validate_qualification_scope(scope):
    receiver = (scope.get('receiver') or '').strip()
    if not receiver:
        raise ValueError('资质豁免必须指定领用人（receiver）')
    police_no = (scope.get('police_no') or '').strip()
    if not police_no:
        raise ValueError('资质豁免必须填写领用人警号（police_no）')
    return receiver, police_no


def evaluate_qualification(ctx, params):
    """领用人必须是已备案且在职的出库人员。"""
    from apps.personnel.models import StockOutPerson
    receiver = (ctx.receiver or '').strip()
    if not receiver:
        return ('领用人不能为空', {'receiver': receiver})
    person = StockOutPerson.objects.filter(name=receiver).first()
    if person is None:
        return (f'领用人「{receiver}」未完成出库资质备案', {'receiver': receiver, 'registered': False})
    if not person.is_active:
        return (f'领用人「{receiver}」资质状态为不在职', {'receiver': receiver, 'is_active': False})
    if ctx.receiver_police_no and person.police_no != ctx.receiver_police_no:
        return (
            f'警号与备案信息不符（备案警号 {person.police_no}）',
            {'provided_police_no': ctx.receiver_police_no, 'registered_police_no': person.police_no},
        )
    return None


def basis_qualification(scope, params):
    """
    资质基础条件：申请时声明的领用人身份（姓名+警号），
    以及该身份当前的备案/在职状态。无资质人员本身也可获批豁免
    （这正是线下例外场景），但身份或备案状态变化必须重新验证。
    """
    receiver, police_no = _validate_qualification_scope(scope)
    from apps.personnel.models import StockOutPerson
    person = StockOutPerson.objects.filter(name=receiver).first()
    if person is not None and person.police_no != police_no:
        raise ValueError('警号与备案信息不符，请确认领用人身份')
    return Basis(
        material={
            'receiver': receiver,
            'police_no': police_no,
            'registered': person is not None,
            'registered_active': bool(person is not None and person.is_active),
        },
        info={
            'registered_police_no': person.police_no if person is not None else None,
        },
    )


@dataclass
class RuleHandler:
    code: str
    category: str
    evaluator: callable
    basis_builder: callable
    scope_validator: callable = None


REGISTRY = {
    RULE_CAPACITY: RuleHandler(
        RULE_CAPACITY, 'capacity', evaluate_capacity, basis_capacity,
        _validate_capacity_scope,
    ),
    RULE_TIME_WINDOW: RuleHandler(
        RULE_TIME_WINDOW, 'time_window', evaluate_time_window, basis_time_window,
    ),
    RULE_QUALIFICATION: RuleHandler(
        RULE_QUALIFICATION, 'qualification', evaluate_qualification, basis_qualification,
        _validate_qualification_scope,
    ),
}

# 数据迁移据此播种默认规则
DEFAULT_RULE_SPECS = [
    {
        'code': RULE_CAPACITY,
        'name': '出库库存容量限制',
        'category': 'capacity',
        'target_action': TARGET_STOCK_OUT,
        'description': '单次出库数量不得超过常规上限（默认 10）。业务高峰线下批准例外时，须申请容量豁免。',
        'params': {'max_qty': 10},
    },
    {
        'code': RULE_TIME_WINDOW,
        'name': '出库允许时段限制',
        'category': 'time_window',
        'target_action': TARGET_STOCK_OUT,
        'description': '每日仅允许在 08:00-20:00 办理出库，其余时段须申请时段豁免。',
        'params': {'start_hour': 8, 'end_hour': 20},
    },
    {
        'code': RULE_QUALIFICATION,
        'name': '领用人资质备案限制',
        'category': 'qualification',
        'target_action': TARGET_STOCK_OUT,
        'description': '领用人必须是已备案且在职的出库人员，否则须申请资质豁免。',
        'params': {},
    },
]

_CONTEXTS = {
    TARGET_STOCK_OUT: StockOutContext,
}


def build_context(action, **kwargs):
    ctx_cls = _CONTEXTS.get(action)
    if ctx_cls is None:
        raise ValueError(f'未知的目标操作：{action}')
    return ctx_cls(**kwargs)


def evaluate_rule(rule, context):
    """对单条规则求值，返回 Violation 或 None。"""
    handler = REGISTRY.get(rule.code)
    if handler is None:
        return None
    result = handler.evaluator(context, rule.params or {})
    if result is None:
        return None
    message, actual = result
    return Violation(
        rule_id=rule.id, rule_code=rule.code, rule_name=rule.name,
        category=rule.category, message=message, actual=actual,
    )


def evaluate_action(action, context):
    """求值某操作下所有启用的规则，返回违规列表。"""
    from .models import GuardRule
    violations = []
    rules = GuardRule.objects.filter(is_active=True, target_action=action)
    for rule in rules:
        violation = evaluate_rule(rule, context)
        if violation is not None:
            violations.append(violation)
    return violations


def validate_scope(rule, scope):
    """校验申请豁免时给出的目标对象范围。"""
    handler = REGISTRY.get(rule.code)
    if handler is None:
        raise ValueError(f'规则 {rule.code} 未注册处理器')
    if handler.scope_validator is not None:
        handler.scope_validator(scope or {})


def build_basis(rule, scope):
    """根据当前基础条件生成快照与指纹。批准时固化、消费时重算。"""
    handler = REGISTRY.get(rule.code)
    if handler is None:
        raise ValueError(f'规则 {rule.code} 未注册处理器')
    return handler.basis_builder(scope or {}, rule.params or {})


def make_ref_key(target_ref):
    """target_ref 的确定性键，用于消费幂等。"""
    return canonical_json(dict(sorted((target_ref or {}).items())))
