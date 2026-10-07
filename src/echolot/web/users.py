"""Users page (admins): everyone who logged in with their Navidrome account, whether they are an admin
here (a Navidrome admin always is; only a Navidrome admin makes someone one) and what else they may do with
their own songs (auth.PERMISSIONS); log someone out everywhere. Accounts gone from Navidrome show as disabled (auth.sync_users)."""

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from echolot.settings import auth
from echolot.web.common import DB, back, page

router = APIRouter(include_in_schema=False)


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, con: DB) -> HTMLResponse:
    rows = auth.users(con)
    sessions = dict(con.execute("SELECT user_id, count(*) FROM sessions GROUP BY user_id").fetchall())
    return page(request, "users.html", nav="users", users=rows, sessions=sessions, permissions=auth.PERMISSIONS)


@router.post("/users/{user_id}")
async def users_save(request: Request, con: DB, user_id: int) -> RedirectResponse:
    """An admin or not (only a Navidrome admin changes that), and the permissions (the checked boxes of the
    form; an admin's stay as stored)."""
    row = con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return back("/users", error="No such user.")
    form = await request.form()
    # a Navidrome admin's box is locked (always an admin): their own Echolot flag stays as it was; and only a
    # Navidrome admin gives or takes admin rights (anyone else's box is locked for the other admins)
    keep = row["navidrome_admin"] or not request.state.user.navidrome_admin
    explicit = bool(row["admin"]) if keep else form.get("admin") == "1"
    stored = {p for p in (row["permissions"] or "").split(",") if p in auth.PERMISSIONS}
    # an admin's permissions are locked on the page (an admin may do everything): what is stored stays, for
    # when they are no admin any more
    locked = explicit or row["navidrome_admin"] or form.get("locked") == "1"  # (boxes not shown as editable)
    permissions = stored if locked else {p for p in auth.PERMISSIONS if form.get(p) == "1"}
    try:
        auth.set_rights(con, user_id, explicit, permissions)
    except auth.AuthError as err:
        return back("/users", error=str(err))
    return back("/users", ok=f"Saved for {row['name']}.")


@router.post("/users/{user_id}/logout")
def users_logout(con: DB, user_id: int, name: Annotated[str, Form()] = "") -> RedirectResponse:
    n = auth.end_sessions(con, user_id)
    return back("/users", ok=f"{name or 'The user'}: {n} login(s) ended.")
