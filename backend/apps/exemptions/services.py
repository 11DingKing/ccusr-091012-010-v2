"""
异常豁免服务层

生命周期：
    create_application -> submit -> approve/reject
    approve 时固化基础条件快照与指纹
    目标操作消费：consume（行锁原子占用名额）-> confirm_usage / fail_usage
    revoke 只对未来生效，已发生的消费记录保留不追溯删除
    revalidate 在基础条件变化时重新校验，失败则豁免失效
"""
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction, IntegrityError
from django.utils import timezone

from .models import Exemption, ExemptionUsage, GuardRule, log_event
from . import rules as rule_engine


class ExemptionError(Exception):
    """豁免业务错误"""


# ==================== 申请与审批 ====================

@transaction.atomic
def create_application(*, rule, applicant, target_action, target_scope,
                       reason, risk_note, mitigation='',
                       max_times=Exemption.USAGE_ONCE, expire_at=None,
                       submit=True):
    """
    创建豁免申请。创建时即校验目标范围并试算基础条件，
    保证申请所关联的对象真实存在。
    """
    if target_action != rule.target_action:
        raise ExemptionError('目标操作与规则适用的操作不一致')
    if not reason or not reason.strip():
        raise ExemptionError('请填写申请事由')
    if not risk_note or not risk_note.strip():
        raise ExemptionError('请填写风险说明')
    if expire_at is None:
        raise ExemptionError('请设置截止时间')
    if timezone.is_naive(expire_at):
        expire_at = timezone.make_aware(expire_at)
    if expire_at <= timezone.now():
        raise ExemptionError('截止时间必须晚于当前时间')
    if max_times < 0:
        raise ExemptionError('有效次数不能为负数')

    # 校验目标范围（如货物是否存在、领用人是否已备案）
    try:
        rule_engine.validate_scope(rule, target_scope or {})
    except ValueError as exc:
        raise ExemptionError(str(exc))

    exemption = Exemption.objects.create(
        rule=rule,
        applicant=applicant,
        target_action=target_action,
        target_scope=target_scope or {},
        reason=reason.strip(),
        risk_note=risk_note.strip(),
        mitigation=mitigation.strip(),
        status=Exemption.STATUS_PENDING if submit else Exemption.STATUS_DRAFT,
        max_times=max_times,
        expire_at=expire_at,
    )
    log_event(exemption, 'submitted' if submit else 'created', actor=applicant,
              detail=reason.strip(),
              payload={'rule_code': rule.code, 'target_scope': exemption.target_scope,
                       'max_times': max_times, 'expire_at': expire_at.isoformat()})
    return exemption


@transaction.atomic
def approve_application(*, exemption_id, approver, remark=''):
    """批准豁免，并固化基础条件快照与指纹。只有批准后的豁免才能被消费。"""
    exemption = Exemption.objects.select_for_update().select_related('rule').get(pk=exemption_id)
    if exemption.status != Exemption.STATUS_PENDING:
        raise ExemptionError('仅待审批状态的豁免可以批准')
    if exemption.is_expired:
        exemption.status = Exemption.STATUS_EXPIRED
        exemption.save(update_fields=['status', 'updated_at'])
        log_event(exemption, 'expired', actor=approver, detail='批准时发现已过截止时间')
        raise ExemptionError('该豁免已过截止时间，无法批准')

    # 批准瞬间按当前基础条件固化指纹
    try:
        basis = rule_engine.build_basis(exemption.rule, exemption.target_scope)
    except ValueError as exc:
        raise ExemptionError(str(exc))

    exemption.status = Exemption.STATUS_APPROVED
    exemption.approver = approver
    exemption.approval_remark = remark.strip()
    exemption.approved_at = timezone.now()
    exemption.basis_snapshot = basis.snapshot
    exemption.basis_fingerprint = rule_engine.fingerprint_of(basis.material)
    exemption.revalidated_at = timezone.now()
    exemption.save()
    log_event(exemption, 'approved', actor=approver, detail=remark.strip(),
              payload={'fingerprint': exemption.basis_fingerprint, 'basis': basis.snapshot})
    return exemption


@transaction.atomic
def reject_application(*, exemption_id, approver, remark=''):
    """拒绝豁免。被拒绝的豁免不能被消费。"""
    exemption = Exemption.objects.select_for_update().get(pk=exemption_id)
    if exemption.status != Exemption.STATUS_PENDING:
        raise ExemptionError('仅待审批状态的豁免可以拒绝')
    exemption.status = Exemption.STATUS_REJECTED
    exemption.approver = approver
    exemption.approval_remark = remark.strip()
    exemption.save()
    log_event(exemption, 'rejected', actor=approver, detail=remark.strip())
    return exemption


@transaction.atomic
def revoke_exemption(*, exemption_id, operator, reason=''):
    """
    撤销豁免。撤销只阻止未来的消费，不追溯删除任何已发生的
    消费记录与目标操作——审计轨迹完整保留。
    """
    exemption = Exemption.objects.select_for_update().get(pk=exemption_id)
    if exemption.status not in (Exemption.STATUS_APPROVED, Exemption.STATUS_PENDING):
        raise ExemptionError('仅已批准或待审批的豁免可以撤销')
    previous = exemption.status
    exemption.status = Exemption.STATUS_REVOKED
    exemption.save()
    log_event(exemption, 'revoked', actor=operator, detail=reason.strip(),
              payload={'previous_status': previous})
    return exemption


@transaction.atomic
def revalidate_exemption(*, exemption_id, actor=None):
    """
    基础条件变化时重新验证：按规则重新计算基础条件指纹，
    与批准时固化的指纹比对；不一致则豁免立即失效，
    已发生的消费仍然保留。
    返回 (exemption, changed: bool)。
    """
    exemption = Exemption.objects.select_for_update().select_related('rule').get(pk=exemption_id)
    if exemption.status != Exemption.STATUS_APPROVED:
        return exemption, False

    try:
        basis = rule_engine.build_basis(exemption.rule, exemption.target_scope)
    except ValueError as exc:
        exemption.status = Exemption.STATUS_REVALIDATION_FAILED
        exemption.save()
        log_event(exemption, 'revalidated', actor=actor,
                  detail=f'基础条件无法重新确认：{exc}', payload={'passed': False})
        return exemption, True

    current_fp = rule_engine.fingerprint_of(basis.material)
    changed = current_fp != exemption.basis_fingerprint
    exemption.revalidated_at = timezone.now()
    if changed:
        exemption.status = Exemption.STATUS_REVALIDATION_FAILED
        exemption.save()
        log_event(exemption, 'revalidated', actor=actor,
                  detail='基础条件较批准时已变化，豁免失效',
                  payload={'passed': False, 'previous_fingerprint': exemption.basis_fingerprint,
                           'current_fingerprint': current_fp, 'current_basis': basis.snapshot})
    else:
        exemption.save(update_fields=['revalidated_at', 'updated_at'])
        log_event(exemption, 'revalidated', actor=actor, detail='基础条件未变化，豁免继续有效',
                  payload={'passed': True, 'fingerprint': current_fp})
    return exemption, changed


# ==================== 目标操作消费 ====================

class ExemptionConsumptionError(ExemptionError):
    def __init__(self, message, violations=None, candidates=None):
        super().__init__(message)
        self.violations = violations or []
        self.candidates = candidates or []


def _find_usable_exemption(action, target_ref, rule):
    """
    查找覆盖该规则与目标对象的已批准可用豁免。
    必须由调用方在事务内持有行锁后再判定名额。
    """
    return (
        Exemption.objects
        .select_for_update()
        .filter(rule=rule, status=Exemption.STATUS_APPROVED, target_action=action)
        .order_by('created_at')
    )


@transaction.atomic
def sweep_stale_exemptions(action, target_ref, violations, actor=None):
    """
    消费前的失效清扫（独立事务提交，不随后续阻断回滚）：
    - 已过截止时间的已批准豁免置为 expired；
    - 覆盖本次操作、但基础条件指纹已变化的豁免置为 revalidation_failed。

    这样即使本次操作因没有有效豁免而被阻断回滚，
    "豁免为何不能再用"的结论依然留痕。
    """
    now = timezone.now()
    expired_ids = list(
        Exemption.objects.filter(
            status=Exemption.STATUS_APPROVED, expire_at__lt=now
        ).values_list('id', flat=True)
    )
    if expired_ids:
        Exemption.objects.filter(pk__in=expired_ids).update(status=Exemption.STATUS_EXPIRED, updated_at=now)
        for exemption in Exemption.objects.filter(pk__in=expired_ids):
            log_event(exemption, 'expired', actor=actor, detail='超过截止时间，自动失效')

    invalidated = []
    for violation in violations:
        rule = GuardRule.objects.get(pk=violation.rule_id)
        candidates = Exemption.objects.filter(
            rule=rule, status=Exemption.STATUS_APPROVED, target_action=action
        ).select_related('rule')
        for exemption in candidates:
            if not exemption.covers_scope(target_ref):
                continue
            try:
                basis = rule_engine.build_basis(rule, exemption.target_scope)
            except ValueError:
                current_fp = None
            else:
                current_fp = rule_engine.fingerprint_of(basis.material)
            if current_fp != exemption.basis_fingerprint:
                exemption.status = Exemption.STATUS_REVALIDATION_FAILED
                exemption.revalidated_at = now
                exemption.save(update_fields=['status', 'revalidated_at', 'updated_at'])
                log_event(exemption, 'revalidated', actor=actor,
                          detail='消费前校验发现基础条件已变化，豁免自动失效',
                          payload={'passed': False, 'current_fingerprint': current_fp})
                invalidated.append(exemption.id)
    return {'expired_ids': expired_ids, 'invalidated_ids': invalidated}


@transaction.atomic
def consume(*, action, target_ref, violations, consumer, context=None, detail=''):
    """
    目标操作在触发规则时调用：为每一条违规匹配并原子消费一个已批准豁免。

    调用前应先执行 sweep_stale_exemptions 提交失效状态。
    - 只有 approved 且未过期、未超次数、范围覆盖、基础条件未变化的豁免可用；
    - 一次性豁免（max_times=1）只能被成功消费一次；
    - 同一业务对象（target_ref）对同一豁免只能消费一次（DB 唯一约束）；
    - 任一条违规没有可用豁免，整体回滚并抛出异常（操作必须被阻断）。

    返回 ExemptionUsage 列表（结果状态为 consumed）。
    """
    if not violations:
        return []

    usages = []
    ref_key = rule_engine.make_ref_key(target_ref)
    unmatched = []

    for violation in violations:
        rule = GuardRule.objects.get(pk=violation.rule_id)
        candidates = _find_usable_exemption(action, target_ref, rule)

        selected = None
        miss_reason = ''
        for exemption in candidates:
            # 过期/名额检查（sweep 已先行提交过期状态；此处兜底）
            if not exemption.is_usable():
                continue
            # 目标对象范围必须覆盖
            if not exemption.covers_scope(target_ref):
                continue
            # 同一业务对象已有消费记录：成功/占用中不可重复使用；
            # 仅"失败且名额已回退"允许同笔业务重试时重新占用
            prior = ExemptionUsage.objects.filter(
                exemption=exemption, target_ref_key=ref_key
            ).first()
            if prior is not None and not (
                prior.result == ExemptionUsage.RESULT_FAILED and prior.quota_refunded
            ):
                miss_reason = '该豁免已用于本次操作，不可重复使用'
                continue
            # 基础条件变化（sweep 已把失效豁免改状态，这里再次确认）
            try:
                basis = rule_engine.build_basis(rule, exemption.target_scope)
                current_fp = rule_engine.fingerprint_of(basis.material)
            except ValueError:
                current_fp = None
            if current_fp != exemption.basis_fingerprint:
                continue
            selected = exemption
            break

        if selected is None:
            unmatched.append({
                'violation': violation.to_dict(),
                'reason': miss_reason or '没有已批准且在有效期内、可覆盖本次操作的豁免',
            })
            continue

        ref_key_q = ExemptionUsage.objects.filter(exemption=selected, target_ref_key=ref_key)
        existing = ref_key_q.first()
        if existing is not None:
            if existing.result == ExemptionUsage.RESULT_FAILED and existing.quota_refunded:
                # 上次目标操作失败、名额已回退：同笔业务重试时重新占用原记录，
                # 不产生重复使用记录
                existing.result = ExemptionUsage.RESULT_CONSUMED
                existing.quota_refunded = False
                existing.finished_at = None
                existing.rule_snapshot = {
                    'id': rule.id, 'code': rule.code, 'name': rule.name,
                    'category': rule.category, 'target_action': rule.target_action,
                    'description': rule.description, 'params': rule.params,
                }
                existing.violation_snapshot = violation.to_dict()
                existing.save()
                selected.used_count += 1
                selected.save(update_fields=['used_count', 'updated_at'])
                log_event(selected, 'consumed', actor=consumer, detail=(detail or '失败后重试重新占用'),
                          payload={'usage_id': existing.id, 'target_ref': target_ref,
                                   'used_count': selected.used_count, 'rearmed': True})
                usages.append(existing)
                continue
            # 已成功或占用中的记录不允许重复使用（一次性豁免不可重复使用的核心保障）
            unmatched.append({
                'violation': violation.to_dict(),
                'reason': '该豁免已用于本次操作，不可重复使用',
            })
            continue

        rule_snapshot = {
            'id': rule.id,
            'code': rule.code,
            'name': rule.name,
            'category': rule.category,
            'target_action': rule.target_action,
            'description': rule.description,
            'params': rule.params,
        }
        exemption_snapshot = {
            'id': selected.id,
            'rule_code': rule.code,
            'reason': selected.reason,
            'risk_note': selected.risk_note,
            'mitigation': selected.mitigation,
            'max_times': selected.max_times,
            'expire_at': selected.expire_at.isoformat(),
            'applicant': selected.applicant.username if selected.applicant else None,
            'approver': selected.approver.username if selected.approver else None,
            'approved_at': selected.approved_at.isoformat() if selected.approved_at else None,
            'approval_remark': selected.approval_remark,
            'basis_snapshot': selected.basis_snapshot,
        }

        try:
            usage = ExemptionUsage.objects.create(
                exemption=selected,
                rule=rule,
                target_action=action,
                target_ref=target_ref,
                target_ref_key=ref_key,
                consumer=consumer,
                result=ExemptionUsage.RESULT_CONSUMED,
                detail=detail,
                rule_snapshot=rule_snapshot,
                exemption_snapshot=exemption_snapshot,
                violation_snapshot=violation.to_dict(),
            )
        except IntegrityError:
            # 并发下同一业务对象同时消费：唯一约束兜底，按阻断处理并回滚
            raise ExemptionConsumptionError(
                '该豁免正在被本次操作的另一请求消费，请重试',
                violations=[violation.to_dict()],
            )
        selected.used_count += 1
        selected.save(update_fields=['used_count', 'updated_at'])
        log_event(selected, 'consumed', actor=consumer, detail=detail,
                  payload={'usage_id': usage.id, 'target_ref': target_ref,
                           'used_count': selected.used_count, 'violation': violation.to_dict()})
        usages.append(usage)

    if unmatched:
        raise ExemptionConsumptionError(
            '存在未被已批准豁免覆盖的规则限制，操作已阻断',
            violations=[item['violation'] for item in unmatched],
            candidates=unmatched,
        )

    return usages


@transaction.atomic
def confirm_usage(*, target_action, target_ref, consumer=None, success=True, detail=''):
    """
    目标操作落地后回填实际使用结果。
    success=True  -> consumed 置为 success；
    success=False -> 置为 failed 并回退名额（一次性豁免可再次被使用）。
    撤销豁免不影响已经完成的结果确认。
    """
    ref_key = rule_engine.make_ref_key(target_ref)
    usages = list(
        ExemptionUsage.objects
        .select_for_update()
        .filter(target_action=target_action, target_ref_key=ref_key,
                result=ExemptionUsage.RESULT_CONSUMED)
    )
    for usage in usages:
        if success:
            usage.result = ExemptionUsage.RESULT_SUCCESS
        else:
            usage.result = ExemptionUsage.RESULT_FAILED
            if not usage.quota_refunded:
                exemption = Exemption.objects.select_for_update().get(pk=usage.exemption_id)
                exemption.used_count = max(0, exemption.used_count - 1)
                exemption.save(update_fields=['used_count', 'updated_at'])
                usage.quota_refunded = True
        usage.detail = (usage.detail + ('\n' + detail if usage.detail and detail else detail)).strip()
        usage.finished_at = timezone.now()
        usage.save()
        log_event(usage.exemption, 'usage_finished', actor=consumer or usage.consumer,
                  detail=detail, payload={'usage_id': usage.id, 'result': usage.result})
    return usages


def audit_record(usage):
    """组装单条审计记录：原规则 + 豁免依据 + 实际使用结果。"""
    return {
        'usage_id': usage.id,
        'consumed_at': usage.consumed_at.isoformat(),
        'finished_at': usage.finished_at.isoformat() if usage.finished_at else None,
        'target_action': usage.target_action,
        'target_ref': usage.target_ref,
        'rule': usage.rule_snapshot or {
            'id': usage.rule_id, 'code': usage.rule.code, 'name': usage.rule.name,
        },
        'violation': usage.violation_snapshot,
        'exemption': usage.exemption_snapshot or {'id': usage.exemption_id},
        'exemption_current_status': usage.exemption.status,
        'usage': {
            'result': usage.result,
            'result_display': usage.get_result_display(),
            'detail': usage.detail,
            'quota_refunded': usage.quota_refunded,
            'consumer': usage.consumer.username if usage.consumer else None,
        },
    }


def get_exemption_or_none(pk):
    try:
        return Exemption.objects.select_related('rule', 'applicant', 'approver').get(pk=pk)
    except ObjectDoesNotExist:
        return None
