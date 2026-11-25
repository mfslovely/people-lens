"""Authentication. The JWT carries only the user id; the role and active status are re-read from the database on every
request, so a token can never carry stale or forged privileges."""

from __future__ import annotations

from fastapi import Header, HTTPException
from pydantic import BaseModel
import jwt

from app.agent.models import Principal, Role
from app.config import get_settings
from app.db import tx


class AuthedUser(BaseModel):
    user_id: int
    employee_id: int
    role: Role
    name: str

    def principal(self) -> Principal:
        return Principal(user_id=self.user_id, employee_id=self.employee_id, role=self.role)


def user_row(employee_id: int) -> dict | None:
    with tx() as conn:
        return conn.execute(
            """SELECT u.id AS user_id, u.role, u.is_active, e.id AS employee_id, e.full_name, e.designation, d.name AS department
                 FROM hr.app_users u JOIN hr.employees e ON e.id=u.employee_id JOIN hr.departments d ON d.id=e.department_id
                WHERE e.id=%s""", (employee_id,)).fetchone()


def current_user(authorization: str = Header(default="")) -> AuthedUser:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    try:
        claims = jwt.decode(authorization[7:], get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise HTTPException(401, "Invalid token") from e
    u = user_row(int(claims["emp"]))
    if not u or not u["is_active"] or str(u["user_id"]) != claims["sub"]:
        raise HTTPException(401, "Identity no longer active")
    return AuthedUser(user_id=u["user_id"], employee_id=u["employee_id"], role=u["role"], name=u["full_name"])
