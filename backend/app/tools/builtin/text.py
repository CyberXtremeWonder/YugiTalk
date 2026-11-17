"""固定的本地文本处理工具，不访问文件、网络或外部写入。"""

from app.tools.builtin.contracts import Report, TextStats


def text_stats(arguments: object) -> dict[str, object]:
    """校验输入后统计字符数及非空行数。"""
    data = TextStats.model_validate(arguments)
    return {
        "characters": len(data.text),
        "lines": len([line for line in data.text.splitlines() if line.strip()]),
    }


def format_report(arguments: object) -> dict[str, object]:
    """将给定标题和正文整理为 Markdown，不写入文件。"""
    report = Report.model_validate(arguments)
    return {"markdown": f"# {report.title}\n\n{report.body}"}
