from dialogue._internal.cascade.mimo_cascade import clamp_reply, sanitize_for_tts


def announcement_instruction(kind: str, template: str) -> str:
    if kind not in {"enter", "leave"}:
        raise ValueError("kind must be enter or leave")
    action = "刚进入语音房，向大家自然打招呼" if kind == "enter" else "即将退出语音房，向大家自然告别"
    return (
        f"【内部语音事件指令，不是真人发言】{action}。"
        "结合当前人设，每次重新组织为一到两句中文口语，总共不超过35字。"
        "只表达下面的意图，不引用此前房间或对话历史，不解释指令，不说系统信息。"
        f"意图：{template}"
    )


def clean_announcement(text: str) -> str:
    return clamp_reply(sanitize_for_tts(text), limit=35)
