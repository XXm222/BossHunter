"""Recruiting pacing fields shared by the UI, validation and startup snapshots."""
import math

from bosshunter.config import DEFAULTS


FIELDS = [
    {"key": "read_delay_min", "label": "读取操作间隔下限", "unit": "秒", "min": 1},
    {"key": "read_delay_max", "label": "读取操作间隔上限", "unit": "秒", "min": 1},
    {"key": "read_page_delay", "label": "分页间隔下限", "unit": "秒", "min": 1},
    {"key": "read_page_delay_max", "label": "分页间隔上限", "unit": "秒", "min": 1},
    {"key": "greet_delay_min", "label": "主动招呼间隔下限", "unit": "秒", "min": 1},
    {"key": "greet_delay_max", "label": "主动招呼间隔上限", "unit": "秒", "min": 1},
    {"key": "greet_per_job_min", "label": "每岗每轮招呼人数下限", "unit": "人", "min": 1, "integer": True},
    {"key": "greet_per_job_max", "label": "每岗每轮招呼人数上限", "unit": "人", "min": 1, "integer": True},
    {"key": "monitor_interval_seconds", "label": "HTTP 轮询间隔", "unit": "秒", "min": 60},
]
RANGES = [("read_delay_min", "read_delay_max"), ("read_page_delay", "read_page_delay_max"),
          ("greet_delay_min", "greet_delay_max"), ("greet_per_job_min", "greet_per_job_max")]


def pacing_values(config):
    values = {**DEFAULTS['recruiting'], 'monitor_interval_seconds': 600,
              **(config.get('recruiting') or {})}
    values['monitor_interval_seconds'] = max(60, values['monitor_interval_seconds'])
    return {field['key']: values[field['key']] for field in FIELDS}


def validate_pacing(values):
    if not isinstance(values, dict) or set(values) != {field['key'] for field in FIELDS}:
        raise ValueError('请提交完整的招聘端节流参数，不接受其他配置字段')
    for field in FIELDS:
        value = values[field['key']]
        try:
            finite = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            finite = False
        if (not finite
                or value < field['min'] or (field.get('integer') and type(value) is not int)):
            suffix = '，且必须为整数' if field.get('integer') else ''
            raise ValueError(f"{field['label']}至少为 {field['min']} {field['unit']}{suffix}")
    for low, high in RANGES:
        if values[low] > values[high]:
            label = next(field['label'] for field in FIELDS if field['key'] == low)
            raise ValueError(f'{label}不能大于上限')
    return dict(values)
