"""Flask-SQLAlchemy + CRUD 的最小示例。

运行前准备：
- 确认已安装 flask-sqlalchemy。
- 默认使用 sqlite 文件 `crud_example.db`，无需额外配置。
- 如需其他数据库，修改 `db_uri` 并提供对应驱动。
"""

from __future__ import annotations

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import Mapped, mapped_column

from flask_sqlalchemy_crud import CRUD


def create_app(db_uri: str) -> tuple[Flask, SQLAlchemy]:
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db = SQLAlchemy(app)
    return app, db


def main() -> None:
    db_uri = "sqlite:///./crud_example.db"
    app, db = create_app(db_uri)

    class User(db.Model):  # type: ignore[misc]
        __tablename__ = "example_user"
        id: Mapped[int] = mapped_column(primary_key=True)
        email: Mapped[str] = mapped_column(db.String(255), unique=True, nullable=False)

    with app.app_context():
        db.drop_all()
        db.create_all()

        CRUD.configure(session=db.session)

        with CRUD(User) as crud:
            user = crud.add(email="demo@example.com")
            print("created:", user)

        with CRUD(User, email="demo@example.com") as crud:
            fetched = crud.first()
            print("fetched:", fetched)


if __name__ == "__main__":
    main()
