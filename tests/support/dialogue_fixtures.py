"""用真实行为策略装配公开生成工厂；旧供应商回归尚有私有属性断言。"""
from behavior import create_reply_policy
from dialogue import create_aliyun_backend, create_gemini_backend, create_mimo_backend

live_connector = None


def mimo_backend(settings, memory):
    return create_mimo_backend(settings, memory, reply_policy=create_reply_policy())


def gemini_backend(settings, memory, **kwargs):
    return create_gemini_backend(settings, memory, reply_policy=create_reply_policy(),
                                 connector=kwargs.pop("connector", live_connector), **kwargs)


def aliyun_backend(settings, memory):
    return create_aliyun_backend(settings, memory, reply_policy=create_reply_policy())
