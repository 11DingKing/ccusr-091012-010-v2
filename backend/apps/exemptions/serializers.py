"""
异常豁免序列化器
"""
from rest_framework import serializers

from .models import Exemption, ExemptionEvent, ExemptionUsage, GuardRule


class GuardRuleSerializer(serializers.ModelSerializer):
    """守护规则序列化器"""
    category_display = serializers.CharField(source='get_category_display', read_only=True)

    class Meta:
        model = GuardRule
        fields = [
            'id', 'code', 'name', 'category', 'category_display',
            'target_action', 'description', 'params',
            'is_active', 'created_at', 'updated_at',
        ]


class ExemptionEventSerializer(serializers.ModelSerializer):
    """豁免生命周期事件"""
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    actor_name = serializers.CharField(source='actor.username', read_only=True)

    class Meta:
        model = ExemptionEvent
        fields = ['id', 'type', 'type_display', 'actor', 'actor_name', 'detail', 'payload', 'created_at']


class ExemptionSerializer(serializers.ModelSerializer):
    """豁免详情序列化器"""
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    rule_code = serializers.CharField(source='rule.code', read_only=True)
    rule_name = serializers.CharField(source='rule.name', read_only=True)
    rule_category = serializers.CharField(source='rule.category', read_only=True)
    rule_category_display = serializers.SerializerMethodField()
    applicant_name = serializers.CharField(source='applicant.username', read_only=True)
    approver_name = serializers.CharField(source='approver.username', read_only=True)
    remaining_times = serializers.SerializerMethodField()
    is_one_time = serializers.BooleanField(read_only=True)
    is_expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = Exemption
        fields = [
            'id', 'rule', 'rule_code', 'rule_name', 'rule_category', 'rule_category_display',
            'applicant', 'applicant_name', 'approver', 'approver_name',
            'target_action', 'target_scope',
            'reason', 'risk_note', 'mitigation',
            'status', 'status_display',
            'max_times', 'used_count', 'remaining_times', 'is_one_time',
            'expire_at', 'is_expired',
            'basis_fingerprint', 'basis_snapshot',
            'approval_remark', 'approved_at', 'revalidated_at',
            'created_at', 'updated_at',
        ]

    def get_rule_category_display(self, obj):
        return obj.rule.get_category_display()

    def get_remaining_times(self, obj):
        return obj.remaining_times


class ExemptionCreateSerializer(serializers.Serializer):
    """豁免申请创建序列化器"""
    rule = serializers.PrimaryKeyRelatedField(
        queryset=GuardRule.objects.all(), error_messages={'required': '请选择被绕过的规则', 'null': '请选择被绕过的规则'}
    )
    target_scope = serializers.DictField(required=False, default=dict)
    reason = serializers.CharField(min_length=2, max_length=500, required=True, error_messages={
        'required': '请填写申请事由', 'blank': '申请事由不能为空',
        'min_length': '申请事由至少2个字', 'max_length': '申请事由最多500个字',
    })
    risk_note = serializers.CharField(min_length=2, max_length=500, required=True, error_messages={
        'required': '请填写风险说明', 'blank': '风险说明不能为空',
        'min_length': '风险说明至少2个字', 'max_length': '风险说明最多500个字',
    })
    mitigation = serializers.CharField(max_length=500, required=False, default='', allow_blank=True)
    max_times = serializers.IntegerField(min_value=0, required=False, default=1)
    expire_at = serializers.DateTimeField(required=True, error_messages={'required': '请设置截止时间'})


class ExemptionRemarkSerializer(serializers.Serializer):
    """审批意见"""
    remark = serializers.CharField(max_length=500, required=False, default='', allow_blank=True)


class ExemptionRevokeSerializer(serializers.Serializer):
    """撤销原因"""
    reason = serializers.CharField(min_length=2, max_length=500, required=True, error_messages={
        'required': '请填写撤销原因', 'blank': '撤销原因不能为空',
    })


class ExemptionUsageSerializer(serializers.ModelSerializer):
    """豁免使用记录序列化器"""
    result_display = serializers.CharField(source='get_result_display', read_only=True)
    consumer_name = serializers.CharField(source='consumer.username', read_only=True)
    exemption_status = serializers.CharField(source='exemption.status', read_only=True)

    class Meta:
        model = ExemptionUsage
        fields = [
            'id', 'exemption', 'exemption_status', 'rule',
            'target_action', 'target_ref', 'target_ref_key',
            'consumer', 'consumer_name',
            'result', 'result_display', 'detail', 'quota_refunded',
            'rule_snapshot', 'exemption_snapshot', 'violation_snapshot',
            'consumed_at', 'finished_at',
        ]
