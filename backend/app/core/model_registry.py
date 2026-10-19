"""集中注册领域实体，供应用装配和测试建立完整数据库元数据。"""

from app.agents import models as agents_models  # noqa: F401
from app.audit import models as audit_models  # noqa: F401
from app.chat import models as chat_models  # noqa: F401
from app.identity import models as identity_models  # noqa: F401
from app.memory import models as memory_models  # noqa: F401
from app.observability import trace_models  # noqa: F401
from app.rag import graph_models as graph_models  # noqa: F401
from app.rag import models as rag_models  # noqa: F401
from app.skills import models as skills_models  # noqa: F401
from app.tasks import ledger_models  # noqa: F401
from app.tasks import models as tasks_models  # noqa: F401
from app.tools import models as tools_models  # noqa: F401
