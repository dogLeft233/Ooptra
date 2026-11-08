"""用真实行为策略装配公开生成工厂；旧供应商回归尚有私有属性断言。"""
from dialogue import create_aliyun_backend, create_gemini_backend, create_mimo_backend

live_connector = None


def mimo_backend(settings):
    return create_mimo_backend(settings)


def gemini_backend(settings, **kwargs):
    return create_gemini_backend(settings,
                                 connector=kwargs.pop("connector", live_connector), **kwargs)


def aliyun_backend(settings):
    return create_aliyun_backend(settings)
