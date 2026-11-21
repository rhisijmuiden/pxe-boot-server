"""First-run wizard: install.sh creates "admin" with a generated password and sets onboarding."""
import re

from fastapi.testclient import TestClient

from app import auth, db
from app.main import app


def csrf(client, path):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def test_generated_admin_goes_through_wizard():
    db.run("DELETE FROM users")
    auth.create_user("admin", "abcd-efgh-jkmn-pqrs", role="admin")
    db.set_setting("onboarding", True)
    c = TestClient(app)
    r = c.post("/login", data={"csrf": csrf(c, "/login"), "username": "admin", "password": "abcd-efgh-jkmn-pqrs"},
               follow_redirects=False)
    assert r.status_code == 303
    # Every page leads to the wizard until it is finished; the API keeps working.
    assert c.get("/library", follow_redirects=False).headers["location"] == "/welcome"
    page = c.get("/welcome")
    assert page.status_code == 200 and 'name="password"' in page.text
    tok = csrf(c, "/welcome")
    # The generated password cannot be kept.
    r = c.post("/welcome", data={"csrf": tok, "username": "admin", "password": "abcd-efgh-jkmn-pqrs",
                                 "password2": "abcd-efgh-jkmn-pqrs", "lang": "nl"}, follow_redirects=False)
    assert r.headers["location"] == "/welcome" and db.get_setting("onboarding")
    r = c.post("/welcome", data={"csrf": tok, "username": "reinder", "name": "Reinder", "password": "my-own-pass",
                                 "password2": "my-own-pass", "lang": "nl", "server_name": "pxe1"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/network")
    assert not db.get_setting("onboarding")
    assert db.get_setting("server_name") == "pxe1" and db.get_setting("default_lang") == "nl"
    assert auth.authenticate("reinder", "my-own-pass") is not None
    assert c.get("/library", follow_redirects=False).status_code == 200
    assert c.get("/welcome", follow_redirects=False).headers["location"] == "/"
