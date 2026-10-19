"""受控的只读工具执行入口，不执行动态代码或外部写入。"""

from app.tools.contracts import Report, TextStats


def execute_tool(name: str, arguments: object) -> dict[str, object]:
    """注册表只允许固定工具，不使用 eval、Shell 或动态模块导入。"""
    if name == "text_stats":
        data = TextStats.model_validate(arguments)
        return {
            "characters": len(data.text),
            "lines": len([line for line in data.text.splitlines() if line.strip()]),
        }
    if name == "format_report":
        report = Report.model_validate(arguments)
        return {"markdown": f"# {report.title}\n\n{report.body}"}
    raise ValueError("工具未注册")
