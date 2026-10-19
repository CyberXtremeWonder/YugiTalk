"""迁移从统一配置读取连接串，不在仓库保存密码。"""

from alembic import context
from app.core.config import load_settings
from sqlalchemy import create_engine, pool

url = (
    load_settings()
    .database_url.get_secret_value()
    .replace("postgresql://", "postgresql+psycopg://", 1)
)
if context.is_offline_mode():
    context.configure(url=url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
