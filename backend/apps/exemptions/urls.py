"""
异常豁免管理URL配置
"""
from django.urls import path

from .views import (
    GuardRuleListView,
    ExemptionListView, ExemptionDetailView,
    ExemptionApproveView, ExemptionRejectView, ExemptionRevokeView,
    ExemptionRevalidateView, ExemptionEventListView, ExemptionUsageListView,
    GuardCheckView, ExemptionAuditView,
)

urlpatterns = [
    # 被守护的规则
    path('guard-rules/', GuardRuleListView.as_view(), name='guard-rule-list'),
    # 操作前试算
    path('guard-check/', GuardCheckView.as_view(), name='guard-check'),

    # 豁免申请
    path('exemptions/', ExemptionListView.as_view(), name='exemption-list'),
    path('exemptions/<int:pk>/', ExemptionDetailView.as_view(), name='exemption-detail'),
    path('exemptions/<int:pk>/approve/', ExemptionApproveView.as_view(), name='exemption-approve'),
    path('exemptions/<int:pk>/reject/', ExemptionRejectView.as_view(), name='exemption-reject'),
    path('exemptions/<int:pk>/revoke/', ExemptionRevokeView.as_view(), name='exemption-revoke'),
    path('exemptions/<int:pk>/revalidate/', ExemptionRevalidateView.as_view(), name='exemption-revalidate'),
    path('exemptions/<int:pk>/events/', ExemptionEventListView.as_view(), name='exemption-events'),
    path('exemptions/<int:pk>/usages/', ExemptionUsageListView.as_view(), name='exemption-usages'),

    # 审计查询：原规则 + 豁免依据 + 实际使用结果
    path('exemption-audit/', ExemptionAuditView.as_view(), name='exemption-audit'),
]
