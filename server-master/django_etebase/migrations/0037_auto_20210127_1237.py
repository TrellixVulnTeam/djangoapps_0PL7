# Generated by Django 3.1.1 on 2021-01-27 12:37

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('django_etebase', '0036_auto_20201214_1128'),
    ]

    operations = [
        migrations.AlterField(
            model_name='collectiontype',
            name='uid',
            field=models.BinaryField(db_index=True, editable=True, max_length=1024, unique=True),
        ),
    ]
