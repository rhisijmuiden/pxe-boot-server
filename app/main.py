"""PXE Boot Server web application."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
import logging
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import answers, auth, catalog, config, db, dnsmasq, i18n, ipxe, isos, live, logs, mailer, proxmox, rules, system, uploads, windows

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("pxe")

db.migrate()


def _secret() -> str:
    try:
        return config.SECRET_FILE.read_text().strip()
    except OSError:
        key = secrets.token_urlsafe(48)
        try:
            config.SECRET_FILE.write_text(key)
            config.SECRET_FILE.chmod(0o600)
        except OSError:
            pass
        return key


@asynccontextmanager
async def _lifespan(_app):
    uploads.cleanup()
    isos.scan_async()
    live.start()
    catalog.start()
    yield


app = FastAPI(title="PXE Boot Server", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
app.add_middleware(SessionMiddleware, secret_key=_secret(), session_cookie="pxe_session", same_site="lax",
                   max_age=14 * 86400, https_only=False)
app.mount("/static", StaticFiles(directory=config.APP_DIR / "static"), name="static")
if config.DEV:
    # nginx serves these in production
    for prefix, path in (("/mnt", config.MNT_DIR), ("/isos", config.ISO_DIR), ("/boot", config.BOOT_DIR),
                         ("/derived", config.DERIVED_DIR)):
        path.mkdir(parents=True, exist_ok=True)
        app.mount(prefix, StaticFiles(directory=path), name=prefix.strip("/"))

templates = Jinja2Templates(directory=config.APP_DIR / "templates")


def fmt_bytes(n: float | int | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def fmt_time(ts: float | None, with_date: bool = True) -> str:
    if not ts:
        return "–"
    lt = time.localtime(ts)
    if not with_date or time.strftime("%Y-%m-%d", lt) == time.strftime("%Y-%m-%d"):
        return time.strftime("%H:%M", lt)
    return time.strftime("%d-%m %H:%M", lt)


templates.env.filters["bytes"] = fmt_bytes
templates.env.filters["time"] = fmt_time
templates.env.globals["METHODS"] = isos.METHODS
templates.env.globals["LANGS"] = i18n.LANGS


# ---------------------------------------------------------------- helpers

def client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "")


def flash(request: Request, key: str, kind: str = "ok", **kw) -> None:
    request.session.setdefault("flash", []).append({"key": key, "kind": kind, "kw": kw})


def render(request: Request, name: str, ctx: dict | None = None, status: int = 200) -> HTMLResponse:
    user = auth.current_user(request)
    lang = i18n.pick(request, user)
    flashes = request.session.pop("flash", [])
    data = {
        "request": request,
        "user": user,
        "lang": lang,
        "t": lambda key, **kw: i18n.translate(lang, key, **kw),
        "csrf": auth.csrf_token(request),
        "version": config.version(),
        "channel": db.get_setting("update_channel"),
        "server_name": db.get_setting("server_name"),
        "flashes": flashes,
        "path": request.url.path,
        "js_i18n": json.dumps({k: v for k, v in i18n.catalog(lang).items() if k.startswith("js_")} |
                              {k: v for k, v in i18n.catalog("en").items() if k.startswith("js_") and
                               k not in i18n.catalog(lang)}),
        "update_badge": request.session.get("update_available", False),
    }
    data.update(ctx or {})
    return templates.TemplateResponse(request, name, data, status_code=status)


@app.exception_handler(auth.LoginRequired)
async def _login_required(request: Request, exc: auth.LoginRequired):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "login"}, status_code=401)
    if auth.user_count() == 0:
        return RedirectResponse("/setup", status_code=303)
    return RedirectResponse("/login?next=" + quote(request.url.path), status_code=303)


async def form_csrf(request: Request) -> dict:
    form = await request.form()
    auth.check_csrf(request, form.get("csrf"))
    return dict(form)


def api_csrf(request: Request) -> None:
    auth.check_csrf(request, request.headers.get("x-csrf-token"))




# ================================================================= PXE client endpoints (no login)

def ctx_from(request: Request) -> dict:
    qp = request.query_params
    mac = (qp.get("mac") or "").lower()
    if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
        mac = ""
    platform = qp.get("platform", "pcbios")
    return {"mac": mac, "platform": "efi" if platform == "efi" else "pcbios", "arch": qp.get("arch", ""),
            "manufacturer": qp.get("manufacturer", "")[:64], "product": qp.get("product", "")[:64],
            "serial": qp.get("serial", "")[:64], "hostname": qp.get("hostname", "")[:64]}


def ipxe_response(text: str) -> PlainTextResponse:
    return PlainTextResponse(text, headers={"Cache-Control": "no-store"})


def host_ip(request: Request) -> str:
    """The address the client used to reach us, so multi-VLAN servers hand out the right URL."""
    host = (request.headers.get("host") or "").split(":")[0]
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host):
        return host
    return system.server_ip()


@app.get("/ipxe/boot.ipxe")
def ipxe_entry(request: Request):
    live.touch(client_ip(request), "ipxe")
    return ipxe_response(ipxe.entry_script(host_ip(request)))


@app.get("/ipxe/menu.ipxe")
def ipxe_menu(request: Request):
    ctx = ctx_from(request)
    ip = client_ip(request)
    machine = db.q1("SELECT * FROM machines WHERE mac=?", ctx["mac"]) if ctx["mac"] else None
    platform_name = "UEFI" if ctx["platform"] == "efi" else "BIOS"
    live.touch(ip, "menu", mac=ctx["mac"], platform=platform_name, arch=ctx["arch"],
               manufacturer=ctx["manufacturer"], product=ctx["product"], serial=ctx["serial"],
               hostname=(machine["name"] if machine and machine["name"] else ctx["hostname"]))
    if machine is not None:
        db.run("UPDATE machines SET last_seen=? WHERE id=?", time.time(), machine["id"])
    rule = rules.effective(ctx["mac"], machine)
    if rules.auto_boot(rule):
        row = isos.get(rule["iso_id"])
        if row is not None and row["status"] == "ready":
            rules.mark_used(ctx["mac"], rule)
            ans = answers.get(rule["answer_id"]) if rule["answer_id"] else None
            return ipxe_response(_boot(request, row, ctx, ans, auto=True))
    db.log_event("menu", mac=ctx["mac"], ip=ip, platform=platform_name, hostname=ctx["hostname"])
    return ipxe_response(ipxe.menu_script(host_ip(request), ctx, rule))


def _boot(request: Request, row, ctx: dict, ans, auto: bool = False) -> str:
    ip = client_ip(request)
    row = isos.ensure_ready(row)
    missing = isos.missing_files(row) if row is not None and row["status"] == "ready" else ["(ISO not mounted)"]
    if missing:
        return ipxe.error_script(
            "Files missing in the mounted ISO: " + ", ".join(missing)[:200] +
            ". Open the ISO library and choose Detect again.",
            ipxe.base(host_ip(request)), ipxe.client_query(ctx))
    platform_name = "UEFI" if ctx["platform"] == "efi" else "BIOS"
    c = live.touch(ip, "loading", mac=ctx["mac"], platform=platform_name, iso_id=row["id"], iso_name=row["name"],
                   answer_id=ans["id"] if ans else None, answer_name=ans["name"] if ans else "")
    c["expected"] = live.expected_bytes(row, ans)
    c["file"] = ""
    db.log_event("auto" if auto else "boot", mac=ctx["mac"], ip=ip, platform=platform_name,
                 hostname=ctx.get("hostname", ""), iso_id=row["id"],
                 iso_name=row["name"] + (f" · {ans['name']}" if ans else ""), answer_id=ans["id"] if ans else None)
    return ipxe.boot_script(host_ip(request), row, ctx, ans)


@app.get("/ipxe/iso/{iso_id}.ipxe")
def ipxe_iso(request: Request, iso_id: int, answer: int | None = None):
    ctx = ctx_from(request)
    row = isos.get(iso_id)
    if row is None or row["status"] != "ready":
        return ipxe_response(ipxe.error_script("This ISO is not available.", ipxe.base(host_ip(request)),
                                               ipxe.client_query(ctx)))
    ans = answers.get(answer) if answer else None
    return ipxe_response(_boot(request, row, ctx, ans))


@app.get("/ipxe/winpe/{iso_id}/{name}")
def ipxe_winpe(request: Request, iso_id: int, name: str):
    row = isos.get(iso_id)
    if row is None:
        raise HTTPException(404)
    ans = None
    aid = request.query_params.get("answer", "")
    if aid.isdigit():
        ans = answers.get(int(aid))
        if ans is not None and ans["format"] != "autounattend":
            ans = None
    files = ipxe.winpe_files(host_ip(request), row, ctx_from(request), ans)
    if name not in files:
        raise HTTPException(404)
    return PlainTextResponse(files[name], headers={"Cache-Control": "no-store"})


@app.api_route("/answers/{aid}/{mac}/{name}", methods=["GET", "POST"])
async def answer_file(request: Request, aid: int, mac: str, name: str):
    row = answers.get(aid)
    if row is None:
        raise HTTPException(404)
    ip = client_ip(request)
    if request.method == "POST" and row["format"] == "proxmox":
        # The Proxmox auto-installer posts its hardware info; use it to find the MAC address.
        try:
            body = await request.json()
            for nic in (body.get("network_interfaces") or []):
                m = (nic.get("mac") or "").lower()
                if db.q1("SELECT 1 FROM machines WHERE mac=?", m):
                    mac = m
                    break
        except (ValueError, AttributeError):
            pass
    files = answers.render(row, ipxe.base(host_ip(request)), mac.replace("-", ":") if mac != "-" else "")
    if name not in files:
        raise HTTPException(404)
    live.touch(ip, "installer", answer_id=aid, answer_name=row["name"])
    return PlainTextResponse(files[name], headers={"Cache-Control": "no-store"})


@app.get("/api/callback/{aid}/{mac}/done")
def install_done(request: Request, aid: int, mac: str):
    ip = client_ip(request)
    row = answers.get(aid)
    already = (live.CLIENTS.get(ip) or {}).get("phase") == "done"
    c = live.touch(ip, "done")
    if already:
        return PlainTextResponse("ok")
    db.log_event("done", mac=mac.replace("-", ":") if mac != "-" else c.get("mac", ""), ip=ip,
                 iso_id=c.get("iso_id"), iso_name=c.get("iso_name") or (row["name"] if row else ""), answer_id=aid)
    mailer.notify("install", f"Installation finished: {c.get('hostname') or ip}",
                  f"{c.get('iso_name', '')} {('· ' + row['name']) if row else ''}\nIP: {ip}\nMAC: {c.get('mac', '')}")
    return PlainTextResponse("ok")


# ================================================================= auth pages

@app.get("/setup")
def setup_page(request: Request):
    if auth.user_count() > 0:
        return RedirectResponse("/login", status_code=303)
    return render(request, "setup.html")


@app.post("/setup")
async def setup_post(request: Request):
    form = await form_csrf(request)
    if auth.user_count() > 0:
        return RedirectResponse("/login", status_code=303)
    if len(form.get("password", "")) < 8 or form.get("password") != form.get("password2"):
        flash(request, "err_password", "error")
        return RedirectResponse("/setup", status_code=303)
    uid = auth.create_user(form.get("username", "admin"), form["password"], name=form.get("name", ""),
                           email=form.get("email", ""), role="admin", lang=form.get("lang", "en"))
    db.set_setting("default_lang", form.get("lang", "en") if form.get("lang") in i18n.LANGS else "en")
    if form.get("server_name"):
        db.set_setting("server_name", form["server_name"].strip()[:64])
    request.session["uid"] = uid
    return RedirectResponse("/network?welcome=1", status_code=303)


@app.get("/login")
def login_page(request: Request):
    if auth.user_count() == 0:
        return RedirectResponse("/setup", status_code=303)
    return render(request, "login.html", {"next": request.query_params.get("next", "/")})


@app.post("/login")
async def login_post(request: Request):
    form = await form_csrf(request)
    user = auth.authenticate(form.get("username", ""), form.get("password", ""))
    if user is None:
        time.sleep(0.5)
        flash(request, "err_login", "error")
        return RedirectResponse("/login", status_code=303)
    request.session.clear()
    request.session["uid"] = user["id"]
    nxt = form.get("next") or "/"
    return RedirectResponse(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    await form_csrf(request)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/forgot")
def forgot_page(request: Request):
    return render(request, "forgot.html", {"mail_ok": mailer.configured()})


@app.post("/forgot")
async def forgot_post(request: Request):
    form = await form_csrf(request)
    who = form.get("email", "").strip().lower()
    row = db.q1("SELECT * FROM users WHERE lower(email)=? OR username=?", who, who) if who else None
    if row and row["email"] and mailer.configured():
        tok = auth.create_reset_token(row["id"])
        link = f"{request.url.scheme}://{request.headers.get('host')}/reset/{tok}"
        lang = row["lang"] or db.get_setting("default_lang")
        t = lambda k: i18n.translate(lang, k)  # noqa: E731
        try:
            mailer.send(row["email"], t("mail_reset_subject"), f"{t('mail_reset_body')}\n\n{link}\n",
                        mailer.html_wrap(t("mail_reset_subject"),
                                         f"<p>{t('mail_reset_body')}</p><p><a href=\"{link}\" style=\"display:inline-block;"
                                         f"background:#FF6B57;color:#14213D;padding:12px 20px;border-radius:10px;"
                                         f"font-weight:700;text-decoration:none\">{t('reset_password')}</a></p>"))
        except Exception as exc:  # noqa: BLE001
            log.warning("reset mail failed: %s", exc)
    flash(request, "reset_sent")
    return RedirectResponse("/login", status_code=303)


@app.get("/reset/{token}")
def reset_page(request: Request, token: str):
    return render(request, "reset.html", {"token": token})


@app.post("/reset/{token}")
async def reset_post(request: Request, token: str):
    form = await form_csrf(request)
    if form.get("password") != form.get("password2") or not auth.use_reset_token(token, form.get("password", "")):
        flash(request, "err_reset", "error")
        return RedirectResponse(f"/reset/{token}", status_code=303)
    flash(request, "reset_done")
    return RedirectResponse("/login", status_code=303)


@app.post("/lang")
async def set_lang(request: Request):
    form = await form_csrf(request)
    lang = form.get("lang") if form.get("lang") in i18n.LANGS else "en"
    user = auth.current_user(request)
    if user:
        db.run("UPDATE users SET lang=? WHERE id=?", lang, user["id"])
    back = form.get("back") or "/"
    resp = RedirectResponse(back if back.startswith("/") and not back.startswith("//") else "/", status_code=303)
    resp.set_cookie("lang", lang, max_age=365 * 86400, samesite="lax")
    return resp


# ================================================================= pages

@app.get("/")
def dashboard(request: Request):
    user = auth.require_user(request)
    rows = isos.list_isos()
    used, total = system.disk_usage()
    since = time.time() - 86400
    boots_today = db.q("SELECT platform, COUNT(*) AS n FROM boot_events WHERE ts>? AND event IN ('boot','auto') "
                       "GROUP BY platform", since)
    clients7 = db.q1("SELECT COUNT(DISTINCT mac) AS n FROM boot_events WHERE ts>? AND mac<>''",
                     time.time() - 7 * 86400)["n"]
    recent = db.q("SELECT * FROM boot_events WHERE event IN ('boot','auto','done') ORDER BY ts DESC LIMIT 6")
    return render(request, "dashboard.html", {
        "nav": "dashboard", "iso_count": len(rows), "folder_count": len(isos.folders()), "used": used, "total": total,
        "boots_today": {r["platform"]: r["n"] for r in boots_today}, "clients7": clients7, "recent": recent,
        "services": system.service_status(), "server_ip": system.server_ip(),
        "interfaces": db.get_setting("interfaces"), "needs_network": not db.get_setting("interfaces"),
    })


@app.get("/live")
def live_page(request: Request):
    auth.require_user(request)
    return render(request, "live.html", {"nav": "live"})


@app.get("/stats")
def stats_page(request: Request):
    auth.require_user(request)
    top = db.q("SELECT iso_name, COUNT(*) AS n FROM boot_events WHERE ts>? AND event IN ('boot','auto') AND iso_name<>'' "
               "GROUP BY iso_name ORDER BY n DESC LIMIT 8", time.time() - 30 * 86400)
    daily = db.get_setting("traffic_daily") or {}
    days = []
    for i in range(13, -1, -1):
        d = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
        days.append({"day": d[5:], "bytes": daily.get(d, 0)})
    installs = db.q1("SELECT COUNT(*) AS n FROM boot_events WHERE ts>? AND event='done'", time.time() - 30 * 86400)["n"]
    boots30 = db.q1("SELECT COUNT(*) AS n FROM boot_events WHERE ts>? AND event IN ('boot','auto')",
                    time.time() - 30 * 86400)["n"]
    return render(request, "stats.html", {"nav": "stats", "top": top, "days": days, "sent_today": live.sent_today(),
                                          "installs30": installs, "boots30": boots30})


@app.get("/api/live")
def api_live(request: Request):
    auth.require_user(request)
    snap = live.snapshot()
    snap["sent_today"] = live.sent_today()
    return snap


@app.get("/library")
def library_page(request: Request):
    auth.require_user(request)
    folder = request.query_params.get("folder", "")
    rows = isos.list_isos()
    ans_count: dict[int, int] = {}
    for a in answers.list_answers():
        ans_count[a["iso_id"]] = ans_count.get(a["iso_id"], 0) + 1
    items = []
    for r in rows:
        f = r["relpath"].rsplit("/", 1)[0] if "/" in r["relpath"] else ""
        if folder and f != folder and not f.startswith(folder + "/"):
            continue
        items.append({"row": r, "folder": f, "method": isos.method_of(r), "tag": isos.os_tag(r),
                      "answers": ans_count.get(r["id"], 0)})
    counts = {}
    for r in rows:
        f = r["relpath"].rsplit("/", 1)[0] if "/" in r["relpath"] else ""
        while f:
            counts[f] = counts.get(f, 0) + 1
            f = f.rsplit("/", 1)[0] if "/" in f else ""
    pending = [dict(r) for r in db.q("SELECT * FROM uploads ORDER BY created")]
    return render(request, "library.html", {"nav": "library", "items": items, "folders": isos.folders(),
                                            "folder": folder, "counts": counts, "total": len(rows),
                                            "pending": pending, "chunk": 32 * 1024 * 1024})


@app.post("/api/library/scan")
def api_scan(request: Request):
    auth.require_user(request)
    api_csrf(request)
    return isos.scan()


@app.patch("/api/isos/{iso_id}")
async def api_iso_update(request: Request, iso_id: int):
    auth.require_user(request)
    api_csrf(request)
    data = await request.json()
    row = isos.get(iso_id)
    if row is None:
        raise HTTPException(404)
    if "name" in data:
        db.run("UPDATE isos SET name=? WHERE id=?", str(data["name"]).strip()[:120] or row["name"], iso_id)
    if "enabled" in data:
        db.run("UPDATE isos SET enabled=? WHERE id=?", 1 if data["enabled"] else 0, iso_id)
    if "method_override" in data:
        m = data["method_override"]
        if m and m not in isos.METHODS:
            raise HTTPException(400)
        db.run("UPDATE isos SET method_override=? WHERE id=?", m or "", iso_id)
    if "folder" in data:
        try:
            isos.move(iso_id, data["folder"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if data.get("redetect"):
        isos.process(iso_id)
    return {"ok": True}


@app.delete("/api/isos/{iso_id}")
def api_iso_delete(request: Request, iso_id: int):
    auth.require_user(request)
    api_csrf(request)
    isos.delete(iso_id)
    return {"ok": True}


@app.post("/api/folders")
async def api_folder_create(request: Request):
    auth.require_user(request)
    api_csrf(request)
    data = await request.json()
    try:
        return {"folder": isos.create_folder(data.get("path", ""))}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/folders")
def api_folder_delete(request: Request, path: str):
    auth.require_user(request)
    api_csrf(request)
    try:
        isos.delete_folder(path)
    except OSError as exc:
        raise HTTPException(400, "not empty") from exc
    return {"ok": True}


# ---------------------------------------------------------------- uploads

@app.post("/api/uploads")
async def api_upload_create(request: Request):
    user = auth.require_user(request)
    api_csrf(request)
    data = await request.json()
    try:
        return uploads.create(data.get("filename", ""), int(data.get("size", 0)), data.get("folder", ""), user["id"])
    except FileExistsError:
        return JSONResponse({"error": "exists"}, status_code=409)
    except OSError:
        return JSONResponse({"error": "disk"}, status_code=507)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/api/uploads/{uid}")
def api_upload_get(request: Request, uid: str):
    auth.require_user(request)
    row = uploads.get(uid)
    if row is None:
        raise HTTPException(404)
    p = uploads.part_path(uid)
    return {"id": uid, "received": p.stat().st_size if p.exists() else 0, "size": row["size"]}


@app.put("/api/uploads/{uid}")
async def api_upload_chunk(request: Request, uid: str, offset: int):
    auth.require_user(request)
    api_csrf(request)
    try:
        received = await uploads.write_chunk(uid, offset, request.stream())
    except KeyError:
        raise HTTPException(404)
    except ValueError as exc:
        return JSONResponse({"error": "offset", "received": exc.args[0]}, status_code=409)
    except OverflowError:
        return JSONResponse({"error": "too large"}, status_code=413)
    return {"received": received}


@app.post("/api/uploads/{uid}/complete")
def api_upload_complete(request: Request, uid: str):
    user = auth.require_user(request)
    api_csrf(request)
    try:
        iso_id = uploads.complete(uid)
    except KeyError:
        raise HTTPException(404)
    except FileExistsError:
        return JSONResponse({"error": "exists"}, status_code=409)
    except ValueError:
        return JSONResponse({"error": "incomplete"}, status_code=400)
    isos.process(iso_id)
    row = isos.get(iso_id)
    mailer.notify("upload", f"ISO uploaded: {row['name']}",
                  f"{user['name'] or user['username']} uploaded {row['relpath']} ({fmt_bytes(row['size'])}).\n"
                  f"Boot method: {isos.METHODS[isos.method_of(row)]['label']}")
    return {"iso_id": iso_id, "status": row["status"], "method": isos.method_of(row)}


@app.delete("/api/uploads/{uid}")
def api_upload_cancel(request: Request, uid: str):
    auth.require_user(request)
    api_csrf(request)
    uploads.cancel(uid)
    return {"ok": True}


# ---------------------------------------------------------------- ISO catalog

@app.get("/catalog")
def catalog_page(request: Request):
    auth.require_user(request)
    groups = []
    for cat in ("linux", "infra", "tools"):
        groups.append({"key": cat, "entries": [e for e in catalog.CATALOG if e["category"] == cat]})
    return render(request, "catalog.html", {"nav": "catalog", "groups": groups, "folders": isos.folders()})


@app.get("/api/catalog/{entry_id}")
def api_catalog_resolve(request: Request, entry_id: str):
    auth.require_user(request)
    try:
        res = catalog.resolve(entry_id)
    except catalog.ResolveError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    res["in_library"] = catalog.in_library(res["filename"])
    return res


def _queue_response(fn):
    try:
        return {"id": fn()}
    except FileExistsError:
        return JSONResponse({"error": "exists"}, status_code=409)
    except catalog.ResolveError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/catalog/{entry_id}/download")
async def api_catalog_download(request: Request, entry_id: str):
    user = auth.require_user(request)
    api_csrf(request)
    data = await request.json()
    entry = catalog.BY_ID.get(entry_id)
    if entry is None:
        raise HTTPException(404)

    def go():
        res = catalog.resolve(entry_id)
        name = f"{entry['name']} {res['version']}".strip()
        return catalog.queue(url=res["url"], filename=res["filename"], folder=data.get("folder", entry["folder"]),
                             expected=res["hash"], name=name, catalog_id=entry_id, archive=entry.get("archive", ""),
                             user_id=user["id"])
    return _queue_response(go)


@app.post("/api/downloads")
async def api_download_url(request: Request):
    user = auth.require_user(request)
    api_csrf(request)
    data = await request.json()
    url = str(data.get("url", "")).strip()
    filename = str(data.get("filename", "")).strip()
    archive = ""
    if not filename:
        from urllib.parse import unquote, urlparse
        filename = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    if filename.lower().endswith(".iso.zip"):
        filename, archive = filename[:-4], "zip"
    return _queue_response(lambda: catalog.queue(url=url, filename=filename, folder=str(data.get("folder", "")),
                                                 expected=str(data.get("checksum", "")), archive=archive,
                                                 user_id=user["id"]))


@app.get("/api/downloads")
def api_downloads(request: Request):
    auth.require_user(request)
    return {"downloads": catalog.list_downloads()}


@app.delete("/api/downloads/{did}")
def api_download_cancel(request: Request, did: int):
    auth.require_user(request)
    api_csrf(request)
    catalog.cancel(did)
    return {"ok": True}


@app.post("/api/downloads/{did}/retry")
def api_download_retry(request: Request, did: int):
    auth.require_user(request)
    api_csrf(request)
    catalog.retry(did)
    return {"ok": True}


# ---------------------------------------------------------------- boot menu

@app.get("/menu")
def menu_page(request: Request):
    auth.require_user(request)
    rows = [r for r in isos.list_isos() if r["status"] == "ready"]
    return render(request, "menu.html", {"nav": "bootmenu", "isos": rows,
                                         "timeout": db.get_setting("menu_timeout"),
                                         "default": str(db.get_setting("menu_default")),
                                         "title": db.get_setting("menu_title")})


@app.get("/api/menu/preview")
def api_menu_preview(request: Request, platform: str = "efi"):
    auth.require_user(request)
    ctx = {"mac": "", "platform": "efi" if platform == "efi" else "pcbios", "arch": "x86_64"}
    return PlainTextResponse(ipxe.menu_script(system.server_ip(), ctx))


@app.post("/menu")
async def menu_save(request: Request):
    auth.require_user(request)
    form = await form_csrf(request)
    try:
        db.set_setting("menu_timeout", max(0, min(3600, int(form.get("timeout", 30)))))
    except ValueError:
        pass
    db.set_setting("menu_default", form.get("default", "local"))
    db.set_setting("menu_title", form.get("title", "").strip()[:60])
    flash(request, "saved")
    return RedirectResponse("/menu", status_code=303)


# ---------------------------------------------------------------- answer files

@app.get("/answers")
def answers_page(request: Request):
    auth.require_user(request)
    return render(request, "answers.html", {"nav": "answers", "rows": answers.list_answers(),
                                            "FORMATS": answers.FORMATS})


def _answer_isos():
    res = []
    for r in isos.list_isos():
        fmt = answers.format_for_method(isos.method_of(r))
        if fmt and r["status"] == "ready":
            res.append({"id": r["id"], "name": r["name"], "format": fmt, "label": answers.FORMATS[fmt],
                        "editions": isos.iso_info(r).get("editions", []) if fmt == "autounattend" else []})
    return res


@app.get("/answers/new")
@app.get("/answers/{aid}/edit")
def answer_edit(request: Request, aid: int | None = None):
    auth.require_user(request)
    row = answers.get(aid) if aid else None
    data = answers.data_of(row) if row else dict(answers.DEFAULT_DATA)
    data.pop("root_password_hash", None)
    data.pop("user_password_hash", None)
    data.pop("win_password", None)
    iso_list = _answer_isos()
    pre_iso = row["iso_id"] if row else (int(request.query_params.get("iso", 0)) or (iso_list[0]["id"] if iso_list else 0))
    return render(request, "answer_edit.html", {
        "nav": "answers", "row": row, "data": data, "isos": iso_list, "iso_id": pre_iso,
        "TIMEZONES": answers.TIMEZONES, "LOCALES": answers.LOCALES, "KEYBOARDS": answers.KEYBOARDS,
        "has_root_pw": bool(row and answers.data_of(row)["root_password_hash"]),
        "has_user_pw": bool(row and answers.data_of(row)["user_password_hash"]),
        "has_win_pw": bool(row and answers.data_of(row)["win_password"]),
    })


def _answer_data(form: dict, existing: dict | None) -> dict:
    d = {k: form.get(k, v) for k, v in answers.DEFAULT_DATA.items() if not isinstance(v, bool)}
    d["reboot"] = form.get("reboot") in ("1", "on", "true", True)
    d["win_bypass"] = form.get("win_bypass") in ("1", "on", "true", True)
    d["win_disk"] = "wipe" if form.get("win_disk") == "wipe" else "ask"
    d["win_disk_id"] = str(form.get("win_disk_id") or "0") if str(form.get("win_disk_id") or "0").isdigit() else "0"
    d["win_index"] = str(form.get("win_index") or "1") if str(form.get("win_index") or "1").isdigit() else "1"
    if form.get("win_pw"):
        d["win_password"] = windows.encode_password(form["win_pw"])
    else:
        d["win_password"] = existing.get("win_password", "") if existing else ""
    for field, pw in (("root_password_hash", "root_password"), ("user_password_hash", "user_password")):
        if form.get(pw):
            d[field] = answers.hash_password(form[pw])
        elif existing:
            d[field] = existing.get(field, "")
        else:
            d[field] = ""
    return d


@app.post("/api/answers/preview")
async def api_answer_preview(request: Request):
    auth.require_user(request)
    api_csrf(request)
    form = await request.json()
    iso = isos.get(int(form.get("iso_id") or 0))
    if iso is None:
        return PlainTextResponse("")
    fmt = answers.format_for_method(isos.method_of(iso))
    existing = None
    if form.get("id"):
        r = answers.get(int(form["id"]))
        existing = answers.data_of(r) if r else None
    data = _answer_data(form, existing)
    for field, pw in (("root_password_hash", "root_password"), ("user_password_hash", "user_password")):
        if form.get(pw):
            data[field] = "$6$<hash of the password you typed>"
    if form.get("win_pw"):
        data["win_password"] = "<encoded password you typed>"
    fake = {"id": int(form.get("id") or 0) or 0, "name": form.get("name") or "profile", "format": fmt,
            "data": json.dumps(data), "updated": time.time(), "iso_id": iso["id"]}
    files = answers.render(fake, ipxe.base(system.server_ip()), "")
    return {"format": fmt, "label": answers.FORMATS.get(fmt, ""), "name": answers.preview_name(fmt),
            "text": files.get(answers.preview_name(fmt), "")}


@app.post("/answers/save")
async def answer_save(request: Request):
    auth.require_user(request)
    form = await form_csrf(request)
    aid = int(form.get("id") or 0) or None
    existing = answers.data_of(answers.get(aid)) if aid and answers.get(aid) else None
    if form.get("confirm_wipe") not in ("1", "on"):
        flash(request, "err_confirm_wipe", "error")
        return RedirectResponse(f"/answers/{aid}/edit" if aid else "/answers/new", status_code=303)
    try:
        answers.save(aid, (form.get("name") or "profile").strip()[:60], int(form.get("iso_id") or 0),
                     _answer_data(form, existing))
    except ValueError:
        flash(request, "err_answer_iso", "error")
        return RedirectResponse("/answers/new", status_code=303)
    flash(request, "answer_saved")
    return RedirectResponse("/answers", status_code=303)


@app.get("/answers/{aid}/download")
def answer_download(request: Request, aid: int):
    auth.require_user(request)
    row = answers.get(aid)
    if row is None:
        raise HTTPException(404)
    fname = answers.preview_name(row["format"])
    text = answers.render(row, ipxe.base(system.server_ip()), "").get(fname, "")
    return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.delete("/api/answers/{aid}")
def api_answer_delete(request: Request, aid: int):
    auth.require_user(request)
    api_csrf(request)
    answers.delete(aid)
    return {"ok": True}


# ---------------------------------------------------------------- machines

@app.get("/machines")
def machines_page(request: Request):
    auth.require_user(request)
    rows = db.q("SELECT machines.*, isos.name AS iso_name, answers.name AS answer_name, g.name AS group_name, "
                "gi.name AS group_iso_name, g.mode AS group_mode FROM machines "
                "LEFT JOIN isos ON isos.id=machines.iso_id LEFT JOIN answers ON answers.id=machines.answer_id "
                "LEFT JOIN machine_groups g ON g.id=machines.group_id LEFT JOIN isos gi ON gi.id=g.iso_id "
                "ORDER BY machines.name COLLATE NOCASE, machines.mac")
    recent = db.q("SELECT mac, ip, platform, MAX(ts) AS ts FROM boot_events WHERE mac<>'' GROUP BY mac ORDER BY ts DESC "
                  "LIMIT 20")
    qp = request.query_params
    edit = db.q1("SELECT * FROM machines WHERE id=?", int(qp["edit"])) if qp.get("edit", "").isdigit() else None
    gedit = rules.get_group(int(qp["gedit"])) if qp.get("gedit", "").isdigit() else None
    return render(request, "machines.html", {
        "nav": "machines", "rows": rows, "recent": recent, "edit": edit, "gedit": gedit, "groups": rules.list_groups(),
        "isos": [r for r in isos.list_isos() if r["status"] == "ready"],
        "answers": answers.list_answers(), "prefill_mac": qp.get("mac", ""),
        "open_form": bool(edit or qp.get("new") or qp.get("mac")),
        "open_group": bool(gedit or qp.get("gnew")),
    })


@app.post("/machines/save")
async def machine_save(request: Request):
    auth.require_user(request)
    form = await form_csrf(request)
    mac = form.get("mac", "").strip().lower().replace("-", ":")
    if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
        flash(request, "err_mac", "error")
        return RedirectResponse("/machines?new=1", status_code=303)
    iso_id = int(form.get("iso_id") or 0) or None
    answer_id = int(form.get("answer_id") or 0) or None
    if answer_id:
        a = answers.get(answer_id)
        if a is None or a["iso_id"] != iso_id:
            answer_id = None
    mode = form.get("mode") if form.get("mode") in rules.MODES else "once"
    mid = int(form.get("id") or 0)
    name = form.get("name", "").strip()[:64]
    group_id = int(form.get("group_id") or 0) or None
    if group_id and rules.get_group(group_id) is None:
        group_id = None
    if mid:
        db.run("UPDATE machines SET mac=?, name=?, iso_id=?, answer_id=?, mode=?, used=0, group_id=? WHERE id=?",
               mac, name, iso_id, answer_id, mode, group_id, mid)
    else:
        db.run("INSERT INTO machines(mac,name,iso_id,answer_id,mode,created,group_id) VALUES(?,?,?,?,?,?,?) "
               "ON CONFLICT(mac) DO UPDATE SET name=excluded.name, iso_id=excluded.iso_id, answer_id=excluded.answer_id, "
               "mode=excluded.mode, used=0, group_id=excluded.group_id", mac, name, iso_id, answer_id, mode, time.time(),
               group_id)
    flash(request, "machine_saved")
    return RedirectResponse("/machines", status_code=303)


@app.post("/machines/groups/save")
async def group_save(request: Request):
    auth.require_user(request)
    form = await form_csrf(request)
    gid = int(form.get("id") or 0)
    name = form.get("name", "").strip()[:64]
    try:
        prefixes = rules.normalize_prefixes(form.get("prefixes", ""))
    except ValueError:
        flash(request, "err_prefix", "error")
        return RedirectResponse(f"/machines?gedit={gid}" if gid else "/machines?gnew=1", status_code=303)
    if not name:
        flash(request, "err_group_name", "error")
        return RedirectResponse(f"/machines?gedit={gid}" if gid else "/machines?gnew=1", status_code=303)
    iso_id = int(form.get("iso_id") or 0) or None
    answer_id = int(form.get("answer_id") or 0) or None
    if answer_id:
        a = answers.get(answer_id)
        if a is None or a["iso_id"] != iso_id:
            answer_id = None
    mode = form.get("mode") if form.get("mode") in rules.MODES else "default"
    if gid:
        db.run("UPDATE machine_groups SET name=?, prefixes=?, iso_id=?, answer_id=?, mode=? WHERE id=?",
               name, prefixes, iso_id, answer_id, mode, gid)
        rules.rearm_group(gid)
    else:
        db.run("INSERT INTO machine_groups(name,prefixes,iso_id,answer_id,mode,created) VALUES(?,?,?,?,?,?)",
               name, prefixes, iso_id, answer_id, mode, time.time())
    flash(request, "group_saved")
    return RedirectResponse("/machines", status_code=303)


@app.post("/api/groups/{gid}/rearm")
def api_group_rearm(request: Request, gid: int):
    auth.require_user(request)
    api_csrf(request)
    rules.rearm_group(gid)
    return {"ok": True}


@app.delete("/api/groups/{gid}")
def api_group_delete(request: Request, gid: int):
    auth.require_user(request)
    api_csrf(request)
    db.run("DELETE FROM machine_groups WHERE id=?", gid)
    return {"ok": True}


@app.post("/api/machines/{mid}/rearm")
def api_machine_rearm(request: Request, mid: int):
    auth.require_user(request)
    api_csrf(request)
    db.run("UPDATE machines SET used=0 WHERE id=?", mid)
    return {"ok": True}


@app.delete("/api/machines/{mid}")
def api_machine_delete(request: Request, mid: int):
    auth.require_user(request)
    api_csrf(request)
    db.run("DELETE FROM machines WHERE id=?", mid)
    return {"ok": True}


# ---------------------------------------------------------------- Proxmox

@app.get("/proxmox")
def proxmox_page(request: Request):
    auth.require_user(request)
    s = proxmox.settings()
    return render(request, "proxmox.html", {
        "nav": "proxmox", "s": s, "configured": proxmox.configured(),
        "isos": [r for r in isos.list_isos() if r["status"] == "ready"], "answers": answers.list_answers(),
    })


@app.post("/proxmox/settings")
async def proxmox_settings_save(request: Request):
    auth.require_admin(request)
    form = await form_csrf(request)
    s = proxmox.settings()
    try:
        s["host"] = proxmox.normalize_host(form.get("host", ""))
    except ValueError:
        flash(request, "err_pve_host", "error")
        return RedirectResponse("/proxmox", status_code=303)
    token = form.get("token_id", "").strip()
    if token and not re.fullmatch(r"[^\s!=]+@[^\s!=]+![^\s!=]+", token):
        flash(request, "err_pve_token", "error")
        return RedirectResponse("/proxmox", status_code=303)
    s["token_id"] = token
    if form.get("secret", "").strip():
        s["secret"] = form["secret"].strip()
    if not token:
        s["secret"] = ""
    s["verify"] = bool(form.get("verify"))
    db.set_setting("proxmox", s)
    flash(request, "saved")
    return RedirectResponse("/proxmox", status_code=303)


def _pve(fn):
    try:
        return fn()
    except proxmox.ProxmoxError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.post("/api/proxmox/test")
def api_proxmox_test(request: Request):
    auth.require_user(request)
    api_csrf(request)
    return _pve(proxmox.overview)


@app.get("/api/proxmox/options")
def api_proxmox_options(request: Request, node: str = ""):
    auth.require_user(request)
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", node):
        raise HTTPException(400)
    return _pve(lambda: proxmox.node_options(node))


@app.get("/api/proxmox/vms")
def api_proxmox_vms(request: Request):
    auth.require_user(request)

    def go():
        vms = proxmox.list_vms()
        by_name = {r["name"].lower(): r for r in db.q("SELECT name, mac, mode, used FROM machines WHERE name<>''")}
        for v in vms:
            m = by_name.get(v["name"].lower())
            v["mac"] = m["mac"] if m else ""
            v["url"] = proxmox.console_url(v["node"], v["vmid"])
        return {"vms": vms}
    return _pve(go)


@app.post("/api/proxmox/vms")
async def api_proxmox_create(request: Request):
    auth.require_user(request)
    api_csrf(request)
    data = await request.json()
    row = isos.get(int(data.get("iso_id") or 0))
    if row is None or row["status"] != "ready":
        return JSONResponse({"error": "iso"}, status_code=400)
    try:
        spec = proxmox.validate({
            "name": str(data.get("name", "")).strip(), "node": data.get("node", ""), "storage": data.get("storage", ""),
            "bridge": data.get("bridge", ""), "vlan": data.get("vlan"), "cores": data.get("cores"),
            "memory_mb": int(float(data.get("memory_gb") or 0) * 1024), "disk_gb": data.get("disk_gb"),
            "firmware": data.get("firmware"),
        })
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not ipxe.platform_ok(isos.method_of(row), "efi" if spec["firmware"] == "uefi" else "pcbios"):
        return JSONResponse({"error": "firmware"}, status_code=400)
    answer_id = int(data.get("answer_id") or 0) or None
    if answer_id:
        a = answers.get(answer_id)
        if a is None or a["iso_id"] != row["id"]:
            answer_id = None
    mode = data.get("mode") if data.get("mode") in ("once", "always") else "once"

    def go():
        vm = proxmox.create_vm(spec, row)
        db.run("INSERT INTO machines(mac,name,iso_id,answer_id,mode,created) VALUES(?,?,?,?,?,?) "
               "ON CONFLICT(mac) DO UPDATE SET name=excluded.name, iso_id=excluded.iso_id, "
               "answer_id=excluded.answer_id, mode=excluded.mode, used=0",
               vm["mac"], spec["name"], row["id"], answer_id, mode, time.time())
        s = proxmox.settings()
        s.update({"node": spec["node"], "storage": spec["storage"], "bridge": spec["bridge"]})
        db.set_setting("proxmox", s)
        started = False
        if data.get("start"):
            proxmox.start_vm(vm["node"], vm["vmid"])
            started = True
        return vm | {"started": started, "url": proxmox.console_url(vm["node"], vm["vmid"])}
    return _pve(go)


@app.post("/api/proxmox/vms/{node}/{vmid}/start")
def api_proxmox_start(request: Request, node: str, vmid: int):
    auth.require_user(request)
    api_csrf(request)
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", node):
        raise HTTPException(400)
    return _pve(lambda: (proxmox.start_vm(node, vmid), {"ok": True})[1])


# ---------------------------------------------------------------- network

@app.get("/network")
def network_page(request: Request):
    auth.require_admin(request)
    return render(request, "network.html", {
        "nav": "network", "itfs": system.interfaces(), "selected": db.get_setting("interfaces") or [],
        "s": {k: db.get_setting(k) for k in ("server_ip", "dhcp_mode", "dhcp_range_start", "dhcp_range_end",
                                             "dhcp_gateway", "dhcp_dns", "dhcp_lease", "uefi_loader", "log_dhcp",
                                             "secure_boot")},
        "sb_ready": dnsmasq.secure_boot_ready(),
        "auto_ip": system.server_ip(), "welcome": request.query_params.get("welcome"),
        "conf": dnsmasq.render(),
    })


@app.post("/network")
async def network_save(request: Request):
    auth.require_admin(request)
    form = await request.form()
    auth.check_csrf(request, form.get("csrf"))
    names = {i["name"] for i in system.interfaces()}
    db.set_setting("interfaces", [n for n in form.getlist("interfaces") if n in names])
    ip = form.get("server_ip", "").strip()
    if ip and not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
        flash(request, "err_ip", "error")
        return RedirectResponse("/network", status_code=303)
    db.set_setting("server_ip", ip)
    db.set_setting("dhcp_mode", "full" if form.get("dhcp_mode") == "full" else "proxy")
    for k in ("dhcp_range_start", "dhcp_range_end", "dhcp_gateway", "dhcp_dns", "dhcp_lease"):
        db.set_setting(k, str(form.get(k, "")).strip())
    db.set_setting("uefi_loader", "ipxe.efi" if form.get("uefi_loader") == "ipxe.efi" else "snponly.efi")
    db.set_setting("log_dhcp", bool(form.get("log_dhcp")))
    db.set_setting("secure_boot", bool(form.get("secure_boot")))
    err = dnsmasq.validate_full_range()
    if err:
        flash(request, err, "error")
        return RedirectResponse("/network", status_code=303)
    try:
        dnsmasq.apply()
        flash(request, "network_applied")
    except system.HelperError as exc:
        flash(request, "err_apply", "error", detail=str(exc))
    return RedirectResponse("/network", status_code=303)


@app.post("/network/vlan")
async def network_vlan(request: Request):
    auth.require_admin(request)
    form = await form_csrf(request)
    parent = form.get("parent", "")
    vid = form.get("vlan_id", "")
    addr = form.get("address", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", parent) or not vid.isdigit() or not (1 <= int(vid) <= 4094) or \
            (addr and not re.fullmatch(r"\d+\.\d+\.\d+\.\d+/\d+", addr)):
        flash(request, "err_vlan", "error")
        return RedirectResponse("/network", status_code=303)
    try:
        system.helper("add-vlan", parent, vid, addr or "dhcp")
        name = f"{parent}.{vid}"
        sel = db.get_setting("interfaces") or []
        if name not in sel:
            db.set_setting("interfaces", sel + [name])
        dnsmasq.apply()
        flash(request, "vlan_added", name=name)
    except system.HelperError as exc:
        flash(request, "err_apply", "error", detail=str(exc))
    return RedirectResponse("/network", status_code=303)


@app.post("/network/vlan/delete")
async def network_vlan_delete(request: Request):
    auth.require_admin(request)
    form = await form_csrf(request)
    name = form.get("name", "")
    if re.fullmatch(r"[A-Za-z0-9_-]+\.\d+", name):
        try:
            system.helper("del-vlan", name)
            db.set_setting("interfaces", [i for i in (db.get_setting("interfaces") or []) if i != name])
            dnsmasq.apply()
            flash(request, "vlan_removed", name=name)
        except system.HelperError as exc:
            flash(request, "err_apply", "error", detail=str(exc))
    return RedirectResponse("/network", status_code=303)


# ---------------------------------------------------------------- boot log

@app.get("/bootlog")
def bootlog_page(request: Request):
    auth.require_user(request)
    page = max(1, int(request.query_params.get("page", 1) or 1))
    rows = db.q("SELECT * FROM boot_events ORDER BY ts DESC LIMIT 100 OFFSET ?", (page - 1) * 100)
    known = {r["mac"] for r in db.q("SELECT mac FROM machines")}
    return render(request, "bootlog.html", {"nav": "bootlog", "rows": rows, "page": page, "known": known})


@app.get("/logs")
def logs_page(request: Request):
    auth.require_user(request)
    return render(request, "logs.html", {"nav": "logs"})


@app.get("/api/logs")
def api_logs(request: Request, kind: str = "http", q: str = "", all: int = 0):  # noqa: A002
    auth.require_user(request)
    q = q.strip()[:100]
    if kind == "http":
        return {"kind": kind, "lines": logs.http(300, q, bool(all)), "error": ""}
    if kind not in ("tftp", "dhcp"):
        raise HTTPException(400)
    lines, err = logs.dnsmasq(kind, 300, q)
    return {"kind": kind, "lines": lines, "error": err, "log_dhcp": bool(db.get_setting("log_dhcp"))}


# ---------------------------------------------------------------- users

@app.get("/users")
def users_page(request: Request):
    auth.require_admin(request)
    edit = db.q1("SELECT * FROM users WHERE id=?", int(request.query_params.get("edit", 0) or 0))
    return render(request, "users.html", {"nav": "users", "rows": db.q("SELECT * FROM users ORDER BY username"),
                                          "edit": edit, "open_form": bool(edit or request.query_params.get("new"))})


@app.post("/users/save")
async def users_save(request: Request):
    me = auth.require_admin(request)
    form = await form_csrf(request)
    uid = int(form.get("id") or 0)
    role = form.get("role") if form.get("role") in auth.ROLES else "operator"
    try:
        if uid:
            if uid == me["id"]:
                role = "admin"  # never lock yourself out
            db.run("UPDATE users SET name=?, email=?, role=? WHERE id=?", form.get("name", ""), form.get("email", ""),
                   role, uid)
            if form.get("password"):
                if len(form["password"]) < 8:
                    raise ValueError
                db.run("UPDATE users SET password_hash=? WHERE id=?", auth.hash_password(form["password"]), uid)
        else:
            auth.create_user(form.get("username", ""), form.get("password", ""), name=form.get("name", ""),
                             email=form.get("email", ""), role=role)
    except (ValueError, Exception) as exc:  # sqlite IntegrityError for duplicates
        log.info("user save failed: %s", exc)
        flash(request, "err_user", "error")
        return RedirectResponse("/users?new=1", status_code=303)
    flash(request, "saved")
    return RedirectResponse("/users", status_code=303)


@app.delete("/api/users/{uid}")
def api_user_delete(request: Request, uid: int):
    me = auth.require_admin(request)
    api_csrf(request)
    if uid == me["id"]:
        raise HTTPException(400, "self")
    db.run("DELETE FROM users WHERE id=?", uid)
    return {"ok": True}


@app.post("/account")
async def account_save(request: Request):
    me = auth.require_user(request)
    form = await form_csrf(request)
    if form.get("new_password"):
        if not auth.verify_password(form.get("current_password", ""), me["password_hash"]) or \
                len(form["new_password"]) < 8 or form["new_password"] != form.get("new_password2"):
            flash(request, "err_password", "error")
            return RedirectResponse("/settings#account", status_code=303)
        db.run("UPDATE users SET password_hash=? WHERE id=?", auth.hash_password(form["new_password"]), me["id"])
    db.run("UPDATE users SET name=?, email=? WHERE id=?", form.get("name", me["name"]), form.get("email", me["email"]),
           me["id"])
    flash(request, "saved")
    return RedirectResponse("/settings#account", status_code=303)


# ---------------------------------------------------------------- mail

@app.get("/mail")
def mail_page(request: Request):
    auth.require_admin(request)
    s = db.get_setting("smtp")
    s["password"] = "********" if s.get("password") else ""
    return render(request, "mail.html", {"nav": "mail", "s": s})


def _smtp_from_form(form: dict) -> dict:
    current = db.get_setting("smtp")
    s = {
        "host": form.get("host", "").strip(), "port": int(form.get("port") or 587),
        "security": form.get("security") if form.get("security") in ("starttls", "ssl", "none") else "starttls",
        "verify": form.get("verify") in ("1", "on"), "username": form.get("username", "").strip(),
        "password": current.get("password", "") if form.get("password") == "********" else form.get("password", ""),
        "from_addr": form.get("from_addr", "").strip(), "from_name": form.get("from_name", "").strip(),
        "reply_to": form.get("reply_to", "").strip(), "notify_to": form.get("notify_to", "").strip(),
        "notify_upload": form.get("notify_upload") in ("1", "on"),
        "notify_install": form.get("notify_install") in ("1", "on"),
    }
    return s


@app.post("/mail")
async def mail_save(request: Request):
    auth.require_admin(request)
    form = await form_csrf(request)
    db.set_setting("smtp", _smtp_from_form(form))
    flash(request, "saved")
    return RedirectResponse("/mail", status_code=303)


@app.post("/api/mail/test")
async def api_mail_test(request: Request):
    me = auth.require_admin(request)
    api_csrf(request)
    form = await request.json()
    s = _smtp_from_form(form)
    to = form.get("to") or me["email"]
    if not to:
        return JSONResponse({"error": "no_recipient"}, status_code=400)
    lang = i18n.pick(request, me)
    subject = i18n.translate(lang, "mail_test_subject")
    body = i18n.translate(lang, "mail_test_body")
    try:
        mailer.send(to, subject, body, mailer.html_wrap(subject, f"<p>{body}</p>"), settings=s)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)[:300]}, status_code=502)
    return {"ok": True, "to": to}


# ---------------------------------------------------------------- settings

@app.get("/settings")
def settings_page(request: Request):
    auth.require_user(request)
    return render(request, "settings.html", {"nav": "settings", "server_name_v": db.get_setting("server_name"),
                                             "default_lang": db.get_setting("default_lang"),
                                             "iso_dir": str(config.ISO_DIR)})


@app.post("/settings")
async def settings_save(request: Request):
    auth.require_admin(request)
    form = await form_csrf(request)
    db.set_setting("server_name", (form.get("server_name") or "pxe-boot-server").strip()[:64])
    if form.get("default_lang") in i18n.LANGS:
        db.set_setting("default_lang", form["default_lang"])
    flash(request, "saved")
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/smb-password")
async def smb_password(request: Request):
    auth.require_admin(request)
    await form_csrf(request)
    pw = secrets.token_urlsafe(18)
    try:
        system.helper("smb-password", stdin=pw + "\n")
        db.set_setting("smb_password", pw)
        flash(request, "smb_rotated")
    except system.HelperError as exc:
        flash(request, "err_apply", "error", detail=str(exc))
    return RedirectResponse("/settings", status_code=303)


# ---------------------------------------------------------------- help

@app.get("/help")
def help_page(request: Request):
    auth.require_user(request)
    return render(request, "help.html", {"nav": "help", "server_ip": system.server_ip(),
                                         "uefi": db.get_setting("uefi_loader")})


# ---------------------------------------------------------------- info & updates

@app.get("/api/info")
def api_info(request: Request):
    auth.require_user(request)
    info = system.version_info()
    info["channel"] = db.get_setting("update_channel")
    info["installed"] = fmt_time(_installed_at())
    return info


def _installed_at() -> float | None:
    try:
        return (config.REPO_DIR / "VERSION").stat().st_mtime
    except OSError:
        return None


@app.post("/api/update/check")
async def api_update_check(request: Request):
    auth.require_admin(request)
    api_csrf(request)
    data = await request.json()
    channel = data.get("channel") if data.get("channel") in ("main", "test") else db.get_setting("update_channel")
    if config.DEV:
        return {"channel": channel, "local": system.git("rev-parse", "--short", "HEAD"), "remote": "dev",
                "remote_version": config.version(), "available": False}
    try:
        out = system.helper("check-update", channel, timeout=60)
        res = json.loads(out)
    except (system.HelperError, ValueError) as exc:
        return JSONResponse({"error": str(exc)[:300]}, status_code=502)
    res["channel"] = channel
    request.session["update_available"] = bool(res.get("available"))
    return res


@app.post("/api/update/run")
async def api_update_run(request: Request):
    auth.require_admin(request)
    api_csrf(request)
    data = await request.json()
    channel = data.get("channel") if data.get("channel") in ("main", "test") else db.get_setting("update_channel")
    db.set_setting("update_channel", channel)
    try:
        system.helper("update", channel, timeout=30)
    except system.HelperError as exc:
        return JSONResponse({"error": str(exc)[:300]}, status_code=502)
    request.session["update_available"] = False
    return {"ok": True}


@app.get("/api/update/log")
def api_update_log(request: Request):
    auth.require_admin(request)
    p = config.LOG_DIR / "update.log"
    try:
        text = p.read_text(errors="ignore")[-20000:]
    except OSError:
        text = ""
    return {"log": text, "version": config.version(), "commit": system.git("rev-parse", "--short", "HEAD")}


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": config.version()}
