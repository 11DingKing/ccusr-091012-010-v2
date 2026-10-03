"""
异常豁免序列化器。
"""
from rest_framework import serializers

from .rules import all_rules


class ExemptionApplySerializer(serializers.Serializer):
    """豁免申请入参。"""

    rule_code = serializers.ChoiceField(
        choices=[(d.code, d.name) for d in all_rules()],
        required=True,
        error_messages={'required': '请选择被绕过的规则', 'invalid_choice': '未知的规则编码'},
    )
    goods_id = serializers.IntegerField(required=True, error_messages={'required': '请选择目标货物'})
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True, min_value=0,
    )
    receiver = serializers.CharField(required=False, allow_blank=True, max_length=100)
    receiver_dept = serializers.CharField(required=False, allow_blank=True, max_length=100)
    risk_note = serializers.CharField(
        required=True, min_length=2, max_length=1000,
        error_messages={'required': '必须填写风险说明', 'min_length': '风险说明至少 2 个字'},
    )
    reason = serializers.CharField(required=False, allow_blank=True, max_length=1000)
    max_uses = serializers.IntegerField(required=False, min_value=1, default=1)
    expire_at = serializers.DateTimeField(
        required=True, error_messages={'required': '必须设置截止时间', 'invalid': '截止时间格式不正确'},
    )

    def to_scope(self):
        data = self.validated_data
        return {
            'goods_id': data['goods_id'],
            'quantity': data.get('quantity'),
            'receiver': data.get('receiver', ''),
            'receiver_dept': data.get('receiver_dept', ''),
        }


class ExemptionDecisionSerializer(serializers.Serializer):
    """审批入参。"""

    remark = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class ExemptionRevokeSerializer(serializers.Serializer):
    """撤销入参。"""

    reason = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class StockOutExecuteSerializer(serializers.Serializer):
    """出库办理（豁免消费）入参。"""

    goods_id = serializers.IntegerField(required=True, error_messages={'required': '请选择货物'})
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=True, min_value=0,
        error_messages={'required': '请填写出库数量'},
    )
    receiver = serializers.CharField(required=True, max_length=100,
                                     error_messages={'required': '请登记领用人'})
    receiver_dept = serializers.CharField(required=False, allow_blank=True, max_length=100)
    remark = serializers.CharField(required=False, allow_blank=True, max_length=1000)
    exemption_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list,
    )


def rule_catalog_data():
    """规则目录（供前端选择被绕过的规则）。"""
    return [{'code': d.code, 'name': d.name} for d in all_rules()]
