"""
异常豁免服务层。

所有状态流转与消费规则集中于此，视图只做参数搬运：

* :func:`apply_for_exemption` —— 申请，必须证明目标操作确实违反所申请的规则，
  并固化基础条件快照；
* :func:`approve_exemption` / :func:`reject_exemption` —— 审批，强制申请人与
  审批人职责分离，批准时刷新基础条件快照；
* :func:`revoke_exemption` —— 撤销仅对未来生效，历史消费记录留存；
* :func:`execute_stock_out` —— 目标操作（出库办理）消费豁免的唯一入口，逐规则
  校验，行锁 + 次数 + 时效 + 基础条件复验，全部满足才在同一事务内落库；
* :func:`audit_for_stock_out` / :func:`audit_list` —— 审计聚合：原规则、豁免依据、
  实际使用结果三段同时呈现。
"""
import uuid
from decimal import Decimal

from django.db import transaction, IntegrityError
from django.db.models import F
from django.utils import timezone

from apps.warehouse.models import Goods, StockOut
from apps.personnel.models import StockOutPerson
from .models import Exemption, ExemptionUse
from .rules import get_rule, all_rules


STOCK_OUT_OPERATION = 'stock_out'


class ExemptionError(Exception):
    """豁免业务校验失败（消息可直接返回前端）。"""


# --------------------------------------------------------------------------
# 业务上下文构建
# --------------------------------------------------------------------------

def build_context(operation_type, scope, at_time=None):
    """
    按操作范围参数构建规则求值上下文。

    申请、审批、消费三个环节共用同一构建方式，保证规则看到的口径一致。
    """
    if operation_type != STOCK_OUT_OPERATION:
        raise ExemptionError(f'不支持的目标操作类型：{operation_type}')

    goods_id = scope.get('goods_id')
    if not goods_id:
        raise ExemptionError('缺少目标货物')
    try:
        goods = Goods.objects.get(pk=goods_id)
    except Goods.DoesNotExist:
        raise ExemptionError('目标货物不存在')

    receiver = scope.get('receiver') or ''
    person = StockOutPerson.objects.filter(name=receiver).first() if receiver else None

    return {
        'goods': goods,
        'quantity': scope.get('quantity'),
        'receiver': receiver,
        'receiver_dept': scope.get('receiver_dept', ''),
        'receiver_person': person,
        'at_time': at_time,
    }


def _normalize_scope(scope):
    """规整存入豁免单的范围参数（数量统一转字符串以便 JSON 与比对）。"""
    normalized = dict(scope or {})
    if normalized.get('quantity') is not None:
        normalized['quantity'] = str(normalized['quantity'])
    return normalized


def _scope_matches(exemption_scope, *, goods, quantity, receiver, receiver_dept):
    """目标操作是否落在豁免单声明的范围内。"""
    scope = exemption_scope or {}
    if scope.get('goods_id') is not None and int(scope['goods_id']) != goods.id:
        return False
    if scope.get('receiver') and scope['receiver'] != receiver:
        return False
    # 容量豁免按申报数量授信：实际出库量不得大于申报量
    if scope.get('quantity') is not None and quantity is not None:
        if Decimal(str(quantity)) > Decimal(str(scope['quantity'])):
            return False
    return True


# --------------------------------------------------------------------------
# 申请与审批
# --------------------------------------------------------------------------

def apply_for_exemption(*, applicant, rule_code, scope, risk_note, reason='',
                        max_uses=1, expire_at=None, at_time=None,
                        operation_type=STOCK_OUT_OPERATION):
    """创建豁免申请。规则未被违反时不允许申请。"""
    rule = get_rule(rule_code)
    if rule is None:
        raise ExemptionError(f'未知规则：{rule_code}')
    if not risk_note or not str(risk_note).strip():
        raise ExemptionError('必须填写风险说明')
    try:
        max_uses = int(max_uses)
    except (TypeError, ValueError):
        raise ExemptionError('有效次数必须为正整数')
    if max_uses < 1:
        raise ExemptionError('有效次数至少为 1 次')
    if not expire_at:
        raise ExemptionError('必须设置截止时间')
    if timezone.is_naive(expire_at):
        expire_at = timezone.make_aware(expire_at)
    if expire_at <= timezone.now():
        raise ExemptionError('截止时间必须晚于当前时间')

    scope = _normalize_scope(scope)
    context = build_context(operation_type, scope, at_time=at_time)
    violation = rule.evaluate(context)
    if violation is None:
        raise ExemptionError(f'当前操作未违反「{rule.name}」，无需申请豁免')

    number = 'EX' + timezone.now().strftime('%Y%m%d%H%M%S') + uuid.uuid4().hex[:6].upper()

    exemption = Exemption.objects.create(
        number=number,
        operation_type=operation_type,
        scope=scope,
        rule_code=rule_code,
        risk_note=risk_note,
        reason=reason or '',
        violation_facts=violation.facts,
        base_condition_snapshot=rule.base_condition(context),
        max_uses=max_uses,
        expire_at=expire_at,
        applicant=applicant,
    )
    return exemption


def _decide(exemption, *, status, decider, remark):
    exemption.refresh_from_db()
    if exemption.status != Exemption.STATUS_PENDING:
        raise ExemptionError(f'当前状态为「{exemption.get_status_display()}」，不可审批')
    if exemption.applicant_id and decider.id == exemption.applicant_id:
        raise ExemptionError('申请人不能审批自己的豁免申请（职责分离）')
    if not decider.is_admin:
        raise ExemptionError('仅管理员可审批豁免')

    exemption.status = status
    exemption.decided_by = decider
    exemption.decision_remark = remark or ''
    exemption.decided_at = timezone.now()

    if status == Exemption.STATUS_APPROVED:
        # 批准时刻刷新基础条件快照，作为后续消费复验的基准
        context = build_context(exemption.operation_type, exemption.scope)
        rule = exemption.rule
        if rule is not None:
            exemption.base_condition_snapshot = rule.base_condition(context)

    exemption.save()
    return exemption


def approve_exemption(exemption, approver, remark=''):
    return _decide(exemption, status=Exemption.STATUS_APPROVED,
                   decider=approver, remark=remark)


def reject_exemption(exemption, approver, remark=''):
    return _decide(exemption, status=Exemption.STATUS_REJECTED,
                   decider=approver, remark=remark)


def revoke_exemption(exemption, operator, reason=''):
    """撤销已批准豁免；只影响后续消费，不动历史记录。"""
    exemption.refresh_from_db()
    if not operator.is_admin:
        raise ExemptionError('仅管理员可撤销豁免')
    if exemption.status != Exemption.STATUS_APPROVED:
        raise ExemptionError(f'当前状态为「{exemption.get_status_display()}」，不可撤销')
    exemption.status = Exemption.STATUS_REVOKED
    exemption.revoked_by = operator
    exemption.revoke_reason = reason or ''
    exemption.revoked_at = timezone.now()
    exemption.save(update_fields=[
        'status', 'revoked_by', 'revoke_reason', 'revoked_at', 'updated_at'
    ])
    return exemption


# --------------------------------------------------------------------------
# 目标操作：出库办理（豁免的唯一消费入口）
# --------------------------------------------------------------------------

@transaction.atomic
def execute_stock_out(*, operator, goods_id, quantity, receiver,
                      receiver_dept='', remark='', exemption_ids=None, at_time=None):
    """
    办理一次出库。

    依次执行全部已登记规则；每个被违反的规则都必须由一份"已批准、在有效期内、
    仍有次数、范围匹配、基础条件未变化"的豁免覆盖，否则整笔出库拒绝（不产生
    任何部分消费）。
    """
    try:
        quantity = Decimal(str(quantity))
    except Exception:
        raise ExemptionError('出库数量格式不正确')
    if quantity <= 0:
        raise ExemptionError('出库数量必须大于 0')

    goods = Goods.objects.select_for_update().get(pk=goods_id)
    scope = _normalize_scope({
        'goods_id': goods.id,
        'quantity': quantity,
        'receiver': receiver or '',
        'receiver_dept': receiver_dept or '',
    })
    context = build_context(STOCK_OUT_OPERATION, scope, at_time=at_time)

    violations = [rule.evaluate(context) for rule in all_rules()]
    violations = [v for v in violations if v is not None]

    # 锁定候选豁免（按主键排序加锁，避免并发死锁）
    candidate_ids = [int(i) for i in (exemption_ids or [])]
    candidates = list(
        Exemption.objects.select_for_update()
        .filter(pk__in=candidate_ids)
        .order_by('pk')
    ) if candidate_ids else []
    if len(candidates) != len(set(candidate_ids)):
        raise ExemptionError('存在无效的豁免单')

    consumptions = []  # (exemption, rule, violation)
    for violation in violations:
        rule = get_rule(violation.code)
        matched = [
            e for e in candidates
            if e.rule_code == violation.code and e.operation_type == STOCK_OUT_OPERATION
        ]
        if not matched:
            raise ExemptionError(f'操作违反「{violation.name}」且未提供豁免：{violation.detail}')

        chosen = None
        reject_reasons = []
        for exemption in matched:
            reason = _check_consumable(
                exemption, rule, context,
                goods=goods, quantity=quantity,
                receiver=receiver or '', receiver_dept=receiver_dept or '',
            )
            if reason is None:
                chosen = exemption
                break
            reject_reasons.append(f'豁免单 {exemption.number}：{reason}')
        if chosen is None:
            raise ExemptionError(
                f'违反「{violation.name}」，但没有可用豁免（{violation.detail}）；' + '；'.join(reject_reasons)
            )
        consumptions.append((chosen, rule, violation))

    # 落库：出库记录 + 扣减库存 + 消费记录（同一事务）
    now = at_time or timezone.now()
    if timezone.is_naive(now):
        now = timezone.make_aware(now)
    stock_out = StockOut.objects.create(
        goods=goods,
        operator=operator,
        receiver=receiver or '',
        receiver_dept=receiver_dept or '',
        quantity=quantity,
        status='completed',
        stock_out_time=now,
        remark=remark or '',
    )
    goods.quantity = goods.quantity - quantity
    goods.save(update_fields=['quantity', 'updated_at'])

    uses = []
    for exemption, rule, violation in consumptions:
        use = _consume(exemption, rule, violation, context, stock_out, operator)
        uses.append(use)

    return stock_out, violations, uses


def _check_consumable(exemption, rule, context, *, goods, quantity, receiver, receiver_dept):
    """返回不可消费原因；None 表示可消费。"""
    if exemption.status != Exemption.STATUS_APPROVED:
        return f'状态为「{exemption.get_status_display()}」，仅已批准豁免可使用'
    if exemption.is_expired:
        return f'已于 {timezone.localtime(exemption.expire_at):%Y-%m-%d %H:%M} 过期'
    if exemption.is_exhausted:
        return f'有效次数 {exemption.max_uses} 次已用尽'
    if not _scope_matches(exemption.scope, goods=goods, quantity=quantity,
                          receiver=receiver, receiver_dept=receiver_dept):
        return '目标操作与豁免申报范围不一致（货物/领用人/数量）'
    snapshot = exemption.base_condition_snapshot or {}
    if not rule.base_condition_matches(snapshot, context):
        current = rule.base_condition(context)
        return (
            '基础条件相较批准时已变化，豁免需重新申请验证 '
            f'（批准时：{snapshot}；当前：{current}）'
        )
    return None


def _consume(exemption, rule, violation, context, stock_out, operator):
    try:
        use = ExemptionUse.objects.create(
            exemption=exemption,
            rule_code=rule.code,
            operation_type=STOCK_OUT_OPERATION,
            operation_ref=f'stock_out#{stock_out.id}',
            stock_out=stock_out,
            violation_detail=violation.detail,
            violation_facts=violation.facts,
            base_condition_at_use=rule.base_condition(context),
            consumed_by=operator,
            once=exemption.is_one_time,
        )
    except IntegrityError:
        # 一次性豁免的数据库级唯一约束兜底并发重复消费
        raise ExemptionError(f'一次性豁免 {exemption.number} 已被使用，不可重复使用')

    # 条件更新兜底并发：剩余次数不足时扣减 0 行
    updated = Exemption.objects.filter(
        pk=exemption.pk, used_count__lt=F('max_uses')
    ).update(used_count=F('used_count') + 1)
    if not updated:
        raise ExemptionError(f'豁免单 {exemption.number} 有效次数已用尽')
    exemption.refresh_from_db()
    return use


# --------------------------------------------------------------------------
# 审计聚合
# --------------------------------------------------------------------------

def _use_payload(use):
    return {
        'id': use.id,
        'result': use.result,
        'result_display': use.get_result_display(),
        'operation_ref': use.operation_ref,
        'stock_out': use.stock_out_id,
        'violation_detail': use.violation_detail,
        'violation_facts': use.violation_facts,
        'base_condition_at_use': use.base_condition_at_use,
        'consumed_by_name': use.consumed_by.username if use.consumed_by else None,
        'consumed_at': use.consumed_at,
    }


def exemption_audit_payload(exemption):
    """豁免单审计视图：原规则 + 豁免依据 + 使用结果。"""
    rule = get_rule(exemption.rule_code)
    uses = exemption.uses.select_related('consumed_by').all()
    return {
        'id': exemption.id,
        # 原规则
        'rule_code': exemption.rule_code,
        'rule_name': rule.name if rule else exemption.rule_code,
        # 豁免依据
        'number': exemption.number,
        'status': exemption.status,
        'status_display': exemption.get_status_display(),
        'lifecycle_state': exemption.lifecycle_state,
        'risk_note': exemption.risk_note,
        'reason': exemption.reason,
        'scope': exemption.scope,
        'violation_facts': exemption.violation_facts,
        'base_condition_snapshot': exemption.base_condition_snapshot,
        'max_uses': exemption.max_uses,
        'used_count': exemption.used_count,
        'remaining_uses': exemption.remaining_uses,
        'is_one_time': exemption.is_one_time,
        'expire_at': exemption.expire_at,
        'applicant_name': exemption.applicant.username if exemption.applicant else None,
        'decided_by_name': exemption.decided_by.username if exemption.decided_by else None,
        'decision_remark': exemption.decision_remark,
        'decided_at': exemption.decided_at,
        'revoked_by_name': exemption.revoked_by.username if exemption.revoked_by else None,
        'revoke_reason': exemption.revoke_reason,
        'revoked_at': exemption.revoked_at,
        'created_at': exemption.created_at,
        # 实际使用结果
        'uses': [_use_payload(u) for u in uses],
    }


def audit_for_stock_out(stock_out):
    """
    单笔出库的审计：操作本身 + 每一条被绕过规则及其豁免依据、消费结果。
    """
    uses = list(
        ExemptionUse.objects
        .filter(stock_out=stock_out)
        .select_related('exemption', 'exemption__applicant', 'exemption__decided_by',
                        'consumed_by')
    )
    items = []
    for use in uses:
        rule = get_rule(use.rule_code)
        items.append({
            'rule_code': use.rule_code,
            'rule_name': rule.name if rule else use.rule_code,
            'violation_detail': use.violation_detail,
            'violation_facts': use.violation_facts,
            'exemption': exemption_audit_payload(use.exemption),
            'use': _use_payload(use),
        })
    return {
        'stock_out': {
            'id': stock_out.id,
            'goods_id': stock_out.goods_id,
            'goods_name': stock_out.goods.name,
            'quantity': str(stock_out.quantity),
            'receiver': stock_out.receiver,
            'receiver_dept': stock_out.receiver_dept,
            'operator_name': stock_out.operator.username if stock_out.operator else None,
            'status': stock_out.status,
            'stock_out_time': stock_out.stock_out_time,
            'remark': stock_out.remark,
        },
        'bypassed_rules': items,
        'has_exemption': bool(items),
    }


def audit_list(queryset):
    """豁免清单审计序列化（带使用记录）。"""
    return [exemption_audit_payload(e) for e in queryset]
