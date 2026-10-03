"""
异常豁免视图

规则查询、豁免申请/审批/撤销/重验，以及面向审计的使用记录查询。
审计结果同时包含：原规则快照、豁免依据快照、违规实况与实际使用结果。
"""
import logging

from django.db.models import Count
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from apps.core.response import success_response, error_response
from .models import Exemption, ExemptionEvent, ExemptionUsage, GuardRule
from . import rules as rule_engine
from . import services
from .serializers import (
    ExemptionCreateSerializer, ExemptionEventSerializer, ExemptionRemarkSerializer,
    ExemptionRevokeSerializer, ExemptionSerializer, ExemptionUsageSerializer,
    GuardRuleSerializer,
)

logger = logging.getLogger('apps')


def _paginate(queryset, request):
    page = max(int(request.query_params.get('page', 1)), 1)
    page_size = min(max(int(request.query_params.get('page_size', 10)), 1), 200)
    total = queryset.count()
    items = queryset[(page - 1) * page_size: page * page_size]
    return items, {'total': total, 'page': page, 'page_size': page_size}


# ==================== 守护规则 ====================

class GuardRuleListView(APIView):
    """被守护的规则列表（容量/时段/资质）"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = GuardRule.objects.all().order_by('category', 'code')
        action = request.query_params.get('target_action')
        if action:
            queryset = queryset.filter(target_action=action)
        category = request.query_params.get('category')
        if category:
            queryset = queryset.filter(category=category)
        return success_response(data=GuardRuleSerializer(queryset, many=True).data)


# ==================== 豁免申请 ====================

class ExemptionListView(APIView):
    """豁免申请列表 / 创建申请"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = Exemption.objects.select_related('rule', 'applicant', 'approver')

        status = request.query_params.get('status')
        if status:
            queryset = queryset.filter(status=status)
        rule_id = request.query_params.get('rule')
        if rule_id:
            queryset = queryset.filter(rule_id=rule_id)
        category = request.query_params.get('category')
        if category:
            queryset = queryset.filter(rule__category=category)
        target_action = request.query_params.get('target_action')
        if target_action:
            queryset = queryset.filter(target_action=target_action)

        queryset = queryset.order_by('-created_at')
        items, pager = _paginate(queryset, request)
        return success_response(data={
            'list': ExemptionSerializer(items, many=True).data, **pager
        })

    def post(self, request):
        serializer = ExemptionCreateSerializer(data=request.data)
        if not serializer.is_valid():
            first_error = list(serializer.errors.values())[0]
            if isinstance(first_error, list):
                first_error = first_error[0]
            return error_response(message=str(first_error))

        data = serializer.validated_data
        rule = data['rule']
        try:
            exemption = services.create_application(
                rule=rule,
                applicant=request.user,
                target_action=rule.target_action,
                target_scope=data.get('target_scope') or {},
                reason=data['reason'],
                risk_note=data['risk_note'],
                mitigation=data.get('mitigation', ''),
                max_times=data['max_times'],
                expire_at=data['expire_at'],
            )
        except services.ExemptionError as exc:
            return error_response(message=str(exc))

        logger.info(f"用户 {request.user.username} 提交豁免申请 #{exemption.id}（规则 {rule.code}）")
        return success_response(data=ExemptionSerializer(exemption).data, message='豁免申请已提交')


class ExemptionDetailView(APIView):
    """豁免详情"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        exemption = services.get_exemption_or_none(pk)
        if exemption is None:
            return error_response(message='豁免不存在', code=404)
        return success_response(data=ExemptionSerializer(exemption).data)


class _ApprovalView(APIView):
    permission_classes = [IsAuthenticated]

    def _load(self, request, pk):
        if not request.user.is_admin:
            return None, error_response(message='仅管理员可以审批豁免', code=403)
        exemption = services.get_exemption_or_none(pk)
        if exemption is None:
            return None, error_response(message='豁免不存在', code=404)
        return exemption, None


class ExemptionApproveView(_ApprovalView):
    def post(self, request, pk):
        exemption, err = self._load(request, pk)
        if err is not None:
            return err
        serializer = ExemptionRemarkSerializer(data=request.data)
        serializer.is_valid(raise_exception=False)
        remark = serializer.validated_data.get('remark', '') if serializer.validated_data else ''
        try:
            exemption = services.approve_application(
                exemption_id=pk, approver=request.user, remark=remark
            )
        except services.ExemptionError as exc:
            return error_response(message=str(exc))
        except Exemption.DoesNotExist:
            return error_response(message='豁免不存在', code=404)
        logger.info(f"管理员 {request.user.username} 批准豁免 #{pk}")
        return success_response(data=ExemptionSerializer(exemption).data, message='已批准')


class ExemptionRejectView(_ApprovalView):
    def post(self, request, pk):
        _, err = self._load(request, pk)
        if err is not None:
            return err
        serializer = ExemptionRemarkSerializer(data=request.data)
        serializer.is_valid(raise_exception=False)
        remark = serializer.validated_data.get('remark', '') if serializer.validated_data else ''
        try:
            exemption = services.reject_application(
                exemption_id=pk, approver=request.user, remark=remark
            )
        except services.ExemptionError as exc:
            return error_response(message=str(exc))
        except Exemption.DoesNotExist:
            return error_response(message='豁免不存在', code=404)
        logger.info(f"管理员 {request.user.username} 拒绝豁免 #{pk}")
        return success_response(data=ExemptionSerializer(exemption).data, message='已拒绝')


class ExemptionRevokeView(APIView):
    """撤销豁免：仅对未来生效，已发生的操作与消费记录保留。"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        exemption = services.get_exemption_or_none(pk)
        if exemption is None:
            return error_response(message='豁免不存在', code=404)
        # 申请人本人或管理员可撤销
        if not request.user.is_admin and exemption.applicant_id != request.user.id:
            return error_response(message='仅申请人本人或管理员可以撤销', code=403)
        serializer = ExemptionRevokeSerializer(data=request.data)
        if not serializer.is_valid():
            first_error = list(serializer.errors.values())[0][0]
            return error_response(message=str(first_error))
        try:
            exemption = services.revoke_exemption(
                exemption_id=pk, operator=request.user,
                reason=serializer.validated_data['reason'],
            )
        except services.ExemptionError as exc:
            return error_response(message=str(exc))
        except Exemption.DoesNotExist:
            return error_response(message='豁免不存在', code=404)
        logger.info(f"用户 {request.user.username} 撤销豁免 #{pk}，已发生的使用记录保留")
        return success_response(data=ExemptionSerializer(exemption).data, message='已撤销，历史使用记录不受影响')


class ExemptionRevalidateView(APIView):
    """基础条件变化时主动触发重新验证。"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        exemption = services.get_exemption_or_none(pk)
        if exemption is None:
            return error_response(message='豁免不存在', code=404)
        try:
            exemption, changed = services.revalidate_exemption(exemption_id=pk, actor=request.user)
        except Exemption.DoesNotExist:
            return error_response(message='豁免不存在', code=404)
        message = '基础条件已变化，豁免失效' if changed else '基础条件未变化，豁免继续有效'
        return success_response(data=ExemptionSerializer(exemption).data, message=message)


class ExemptionEventListView(APIView):
    """单个豁免的生命周期事件轨迹"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not Exemption.objects.filter(pk=pk).exists():
            return error_response(message='豁免不存在', code=404)
        events = ExemptionEvent.objects.filter(
            exemption_id=pk
        ).select_related('actor')
        return success_response(data=ExemptionEventSerializer(events, many=True).data)


class ExemptionUsageListView(APIView):
    """单个豁免的实际使用记录"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not Exemption.objects.filter(pk=pk).exists():
            return error_response(message='豁免不存在', code=404)
        usages = ExemptionUsage.objects.filter(exemption_id=pk).select_related('consumer')
        return success_response(data=ExemptionUsageSerializer(usages, many=True).data)


# ==================== 操作前试算 ====================

class GuardCheckView(APIView):
    """
    目标操作前试算：返回当前触发的规则限制，以及是否存在可覆盖的已批准豁免。
    便于前端在提交前提示"将使用哪条豁免 / 因何被阻断"。
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        action = request.data.get('target_action', rule_engine.TARGET_STOCK_OUT)
        try:
            context = self._build_context(action, request.data)
        except ValueError as exc:
            return error_response(message=str(exc))

        violations = rule_engine.evaluate_action(action, context)
        result = []
        for violation in violations:
            usable = []
            for exemption in Exemption.objects.filter(
                rule_id=violation.rule_id, status=Exemption.STATUS_APPROVED
            ).select_related('rule'):
                if exemption.is_usable() and exemption.covers_scope(self._scope_of(action, request.data)):
                    usable.append({'id': exemption.id, 'max_times': exemption.max_times,
                                   'remaining_times': exemption.remaining_times,
                                   'expire_at': exemption.expire_at.isoformat()})
            result.append({
                **violation.to_dict(),
                'usable_exemptions': usable,
                'exempted': bool(usable),
            })
        return success_response(data={
            'blocked': bool(result),
            'violations': result,
        })

    @staticmethod
    def _scope_of(action, data):
        if action == rule_engine.TARGET_STOCK_OUT:
            return {'goods_id': data.get('goods') or data.get('goods_id'),
                    'receiver': data.get('receiver', ''),
                    'police_no': data.get('receiver_police_no', '')}
        return {}

    @staticmethod
    def _build_context(action, data):
        from decimal import Decimal
        from apps.warehouse.models import Goods
        if action != rule_engine.TARGET_STOCK_OUT:
            raise ValueError(f'不支持的目标操作：{action}')
        goods_id = data.get('goods') or data.get('goods_id')
        goods = Goods.objects.filter(pk=goods_id).first() if goods_id else None
        quantity = data.get('quantity')
        return rule_engine.build_context(
            action,
            goods=goods,
            quantity=Decimal(str(quantity)) if quantity is not None else None,
            receiver=data.get('receiver', ''),
            receiver_police_no=data.get('receiver_police_no', ''),
        )


# ==================== 审计查询 ====================

class ExemptionAuditView(APIView):
    """
    豁免审计查询。每一条记录同时展示：
    原规则（rule + violation）、豁免依据（exemption）、实际使用结果（usage）。
    支持按目标操作、业务对象、规则、豁免编号、使用结果与时间范围过滤。
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = ExemptionUsage.objects.select_related(
            'rule', 'exemption', 'consumer'
        ).order_by('-consumed_at')

        params = request.query_params
        if params.get('target_action'):
            queryset = queryset.filter(target_action=params['target_action'])
        if params.get('rule_code'):
            queryset = queryset.filter(rule__code=params['rule_code'])
        if params.get('category'):
            queryset = queryset.filter(rule__category=params['category'])
        if params.get('exemption'):
            queryset = queryset.filter(exemption_id=params['exemption'])
        if params.get('result'):
            queryset = queryset.filter(result=params['result'])
        if params.get('consumer'):
            queryset = queryset.filter(consumer_id=params['consumer'])
        if params.get('stock_out_id'):
            queryset = queryset.filter(target_ref__stock_out_id=int(params['stock_out_id']))
        if params.get('goods_id'):
            queryset = queryset.filter(target_ref__goods_id=int(params['goods_id']))
        if params.get('start'):
            queryset = queryset.filter(consumed_at__gte=params['start'])
        if params.get('end'):
            queryset = queryset.filter(consumed_at__lte=params['end'])

        items, pager = _paginate(queryset, request)
        records = [services.audit_record(usage) for usage in items]

        # 汇总：成功 / 失败 / 占用中
        result_summary = {
            row['result']: row['count']
            for row in queryset.values('result').annotate(count=Count('id'))
        }
        return success_response(data={
            'list': records,
            'result_summary': result_summary,
            **pager,
        })
