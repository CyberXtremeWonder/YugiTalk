"""独立初始化用户与个人空间；密码交互输入，绝不接受命令行明文密码。"""

import argparse
import getpass
import sys

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.core.config import load_settings
from app.core.database import create_database
from app.identity.contracts import EmailInput, LoginInput
from app.identity.models import User
from app.identity.security import hasher


def main() -> int:
    """校验初始化参数，创建或更新用户与所属空间关系。"""
    parser = argparse.ArgumentParser(description="创建用户，不存在默认账号")
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--platform-admin", action="store_true")
    parser.add_argument("--username", help="仅平台管理员可设置独立登录用户名")
    parser.add_argument(
        "--password-stdin", action="store_true", help="自动化从标准输入读取，勿使用命令行参数传密码"
    )
    args = parser.parse_args()
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("输入密码：")
        if password != getpass.getpass("再次输入密码："):
            print("两次密码不一致", file=sys.stderr)
            return 2
    if not password:
        print("密码不能为空", file=sys.stderr)
        return 2
    try:
        data = EmailInput(email=args.email)
        name = args.name.strip()
        if not 1 <= len(name) <= 80:
            raise ValueError("显示名称需要1到80字")
        username = LoginInput(email=args.username, password="").email if args.username else None
        if username and (not args.platform_admin or "@" in username):
            raise ValueError("独立用户名仅限平台管理员")
    except ValueError:
        print("邮箱或名称无效", file=sys.stderr)
        return 2
    try:
        engine = create_database(load_settings())
    except ValueError:
        print("环境配置无效，请执行启动预检；未初始化任何用户。", file=sys.stderr)
        return 2
    try:
        with Session(engine) as db:
            if db.scalar(select(User).where(User.email == data.email)) or (
                username and db.scalar(select(User).where(User.username == username))
            ):
                print("用户已存在，未修改密码或角色", file=sys.stderr)
                return 2
            user = User(
                email=data.email,
                name=name,
                password_hash=hasher.hash(password),
                platform_admin=args.platform_admin,
                username=username,
            )
            db.add(user)
            db.flush()
            db.add(
                AuditEvent(
                    actor_id=user.id,
                    account_id=user.id,
                    action="user.bootstrap",
                    target_id=user.id,
                )
            )
            db.commit()
        print("用户已创建，请使用刚才输入的密码登录。")
        return 0
    except SQLAlchemyError:
        print("初始化失败，请检查数据库连接、迁移或用户重复情况。", file=sys.stderr)
        return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
