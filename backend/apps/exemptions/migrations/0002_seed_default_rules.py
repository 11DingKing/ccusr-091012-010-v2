"""播种默认守护规则：出库容量、时段、资质限制"""
from django.db import migrations


def seed_rules(apps, schema_editor):
    GuardRule = apps.get_model('exemptions', 'GuardRule')
    from apps.exemptions.rules import DEFAULT_RULE_SPECS
    for spec in DEFAULT_RULE_SPECS:
        GuardRule.objects.update_or_create(
            code=spec['code'],
            defaults={
                'name': spec['name'],
                'category': spec['category'],
                'target_action': spec['target_action'],
                'description': spec['description'],
                'params': spec['params'],
                'is_active': True,
            },
        )


def remove_rules(apps, schema_editor):
    GuardRule = apps.get_model('exemptions', 'GuardRule')
    from apps.exemptions.rules import DEFAULT_RULE_SPECS
    GuardRule.objects.filter(code__in=[s['code'] for s in DEFAULT_RULE_SPECS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('exemptions', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed_rules, remove_rules),
    ]
