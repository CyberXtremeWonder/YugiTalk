"""无需模型调用的保守问答分流；无法确认时保留规划闭环。"""

import re


def simple_question(message: str) -> bool:
    """仅识别寒暄和单个概念定义，不把短操作指令当成简单问答。"""
    text = message.strip().rstrip("？?。！!.").strip()
    if text.lower() in {"你好", "您好", "嗨", "hello", "hi", "早上好", "晚上好"}:
        return True
    if len(text) > 80 or any(word in text for word in (
        "研究", "调研", "报告", "规划", "计划", "步骤", "检索", "搜索", "最新",
        "实时", "今天", "当前", "先", "再", "然后", "以及", "同时", "分别", "比较", "对比",
        "生成", "导出", "创建", "修改", "删除", "部署", "发送", "执行", "调用",
        "并", "和", "与", "分析", "证明", "推导", "调试", "修复", "评估",
    )):
        return False
    topic = r"[\w +#./-]{1,60}"
    return bool(re.fullmatch(
        rf"(?:什么是\s*{topic}|{topic}\s*是什么|(?:请)?解释一下\s*{topic})", text,
    ))
