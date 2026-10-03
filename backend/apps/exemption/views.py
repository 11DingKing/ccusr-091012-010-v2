"""
异常豁免视图。

对外提供：规则目录、豁免申请/审批/拒绝/撤销、豁免与出库审计查询，以及目标操作
"出库办理"（豁免的消费入口）。
"""
import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.core.response import success_response, error_response
from apps.warehouse.models import Goods, StockOut

from .models import Exemption
from .serializers import (
    ExemptionApplySerializer, ExemptionDecisionSerializer,
    ExemptionRevokeSerializer, StockOutExecuteSerializer,
    rule_catalog_data,
)
from .services import (
    ExemptionError,
    apply_for_exemption,
    approve_exemption,
    reject_exemption,
    revoke_exemption,
    execute_stock_out,
    exemption_audit_payload,
    audit_for_stock_out,
)

logger = logging.getLogger('apps')


def _first_error(serializer):
    errors = serializer.errors
    value = list(errors.values())[0]
    if isinstance(value, list):
        value = value[0]
    return str(value)


def _get_exemption(pk):
    return Exemption.objects.filter(pk=pk).first()


class RuleCatalogView(APIView):
    """可被豁免的规则目录。"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return success_response(data=rule_catalog_data())


class ExemptionListCreateView(APIView):
    """豁免申请列表（含审计信息）/ 提交申请。"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = (
            Exemption.objects
            .select_related('applicant', 'decided_by', 'revoked_by')
            .prefetch_related('uses')
            .all()
        )
        status = request.query_params.get('status')
        if status:
            queryset = queryset.filter(status=status)
        rule_code = request.query_params.get('rule_code')
        if rule_code:
            queryset = queryset.filter(rule_code=rule_code)
        number = request.query_params.get('number')
        if number:
            queryset = queryset.filter(number__icontains=number)

        queryset = queryset.order_by('-created_at')
        try:
            page = max(1, int(request.query_params.get('page', 1)))
            page_size = max(1, int(request.query_params.get('page_size', 10)))
        except ValueError:
            page, page_size = 1, 10

        total = queryset.count()
        items = [exemption_audit_payload(e) for e in queryset[(page - 1) * page_size:page * page_size]]
        return success_response(data={
            'list': items, 'total': total, 'page': page, 'page_size': page_size,
        })

    def post(self, request):
        serializer = ExemptionApplySerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        try:
            exemption = apply_for_exemption(
                applicant=request.user,
                rule_code=serializer.validated_data['rule_code'],
                scope=serializer.to_scope(),
                risk_note=serializer.validated_data['risk_note'],
                reason=serializer.validated_data.get('reason', ''),
                max_uses=serializer.validated_data.get('max_uses', 1),
                expire_at=serializer.validated_data['expire_at'],
            )
        except ExemptionError as exc:
            return error_response(message=str(exc))

        logger.info('User %s applied exemption %s for rule %s',
                    request.user.username, exemption.number, exemption.rule_code)
        return success_response(data=exemption_audit_payload(exemption), message='豁免申请已提交，等待审批')


class ExemptionDetailView(APIView):
    """豁免单详情（原规则 + 豁免依据 + 实际使用结果）。"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        exemption = _get_exemption(pk)
        if exemption is None:
            return error_response(message='豁免单不存在', code=404)
        return success_response(data=exemption_audit_payload(exemption))


class _DecisionView(APIView):
    """审批/拒绝公共逻辑。"""
    permission_classes = [IsAuthenticated]

    def _handle(self, request, pk, action):
        if not request.user.is_admin:
            return error_response(message='仅管理员可审批豁免', code=403)
        exemption = _get_exemption(pk)
        if exemption is None:
            return error_response(message='豁免单不存在', code=404)

        serializer = ExemptionDecisionSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        remark = serializer.validated_data.get('remark', '')

        try:
            if action == 'approve':
                approve_exemption(exemption, request.user, remark)
                message = '豁免已批准，可被目标操作消费'
            else:
                reject_exemption(exemption, request.user, remark)
                message = '豁免已拒绝'
        except ExemptionError as exc:
            return error_response(message=str(exc))

        logger.info('User %s %s exemption %s', request.user.username, action, exemption.number)
        exemption.refresh_from_db()
        return success_response(data=exemption_audit_payload(exemption), message=message)


class ExemptionApproveView(_DecisionView):
    def post(self, request, pk):
        return self._handle(request, pk, 'approve')


class ExemptionRejectView(_DecisionView):
    def post(self, request, pk):
        return self._handle(request, pk, 'reject')


class ExemptionRevokeView(APIView):
    """撤销豁免：仅影响后续使用，历史操作不追溯删除。"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not request.user.is_admin:
            return error_response(message='仅管理员可撤销豁免', code=403)
        exemption = _get_exemption(pk)
        if exemption is None:
            return error_response(message='豁免单不存在', code=404)

        serializer = ExemptionRevokeSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))

        try:
            revoke_exemption(exemption, request.user,
                             serializer.validated_data.get('reason', ''))
        except ExemptionError as exc:
            return error_response(message=str(exc))

        logger.info('User %s revoked exemption %s', request.user.username, exemption.number)
        exemption.refresh_from_db()
        return success_response(data=exemption_audit_payload(exemption),
                                message='豁免已撤销，已发生的使用记录保留备查')


class StockOutExecuteView(APIView):
    """
    出库办理（目标操作）。

    逐规则校验容量/时段/资质限制；被违反的规则必须由已批准豁免覆盖，否则拒绝
    整笔操作。返回审计片段（原规则、豁免依据、消费结果）。
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = StockOutExecuteSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))

        data = serializer.validated_data
        try:
            stock_out, violations, uses = execute_stock_out(
                operator=request.user,
                goods_id=data['goods_id'],
                quantity=data['quantity'],
                receiver=data.get('receiver', ''),
                receiver_dept=data.get('receiver_dept', ''),
                remark=data.get('remark', ''),
                exemption_ids=data.get('exemption_ids', []),
            )
        except ExemptionError as exc:
            return error_response(message=str(exc))
        except Goods.DoesNotExist:
            return error_response(message='目标货物不存在', code=404)

        logger.info('User %s executed stock_out#%s with %s exemption(s)',
                    request.user.username, stock_out.id, len(uses))
        return success_response(
            data=audit_for_stock_out(StockOut.objects.get(pk=stock_out.id)),
            message='出库办理成功' + (f'，使用 {len(uses)} 份豁免' if uses else ''),
        )


class StockOutAuditView(APIView):
    """单笔出库审计：原规则、豁免依据、实际使用结果同时展示。"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        stock_out = (
            StockOut.objects
            .select_related('goods', 'operator')
            .filter(pk=pk)
            .first()
        )
        if stock_out is None:
            return error_response(message='出库记录不存在', code=404)
        return success_response(data=audit_for_stock_out(stock_out))
