"""
异常豁免 URL 配置
"""
from django.urls import path

from .views import (
    RuleCatalogView,
    ExemptionListCreateView, ExemptionDetailView,
    ExemptionApproveView, ExemptionRejectView, ExemptionRevokeView,
    StockOutExecuteView, StockOutAuditView,
)

urlpatterns = [
    # 规则目录
    path('rules/', RuleCatalogView.as_view(), name='exemption-rules'),

    # 豁免申请与查询
    path('exemptions/', ExemptionListCreateView.as_view(), name='exemption-list'),
    path('exemptions/<int:pk>/', ExemptionDetailView.as_view(), name='exemption-detail'),
    path('exemptions/<int:pk>/approve/', ExemptionApproveView.as_view(), name='exemption-approve'),
    path('exemptions/<int:pk>/reject/', ExemptionRejectView.as_view(), name='exemption-reject'),
    path('exemptions/<int:pk>/revoke/', ExemptionRevokeView.as_view(), name='exemption-revoke'),

    # 目标操作：出库办理（豁免消费入口）与单笔审计
    path('stock-out/execute/', StockOutExecuteView.as_view(), name='stock-out-execute'),
    path('stock-out/<int:pk>/audit/', StockOutAuditView.as_view(), name='stock-out-audit'),
]
