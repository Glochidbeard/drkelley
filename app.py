"""Dr. Kelley — treatment plan builder.

Afflictions (left) get an ordered list of saved treatment templates (right).
Two CSV exports feed Viridian:
  /export/plans.csv      one row per affliction: treatments, frequencies, waits
  /export/templates.csv  one row per template: method, chemical, rate, week, REI
"""
import csv
import io
import os
from datetime import date, timedelta

from flask import Flask, Response, flash, redirect, render_template, request, url_for
from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Integer, MetaData, Table, Text,
    create_engine, event, func, select, insert, update, delete,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REI_CSV = os.path.join(BASE_DIR, "data", "rei_list.csv")

METHODS = ["Drench", "Foliar spray", "Drizzle"]
WEEKS_BACK, WEEKS_AHEAD = 4, 52

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dr-kelley-dev")


# ── Database ──────────────────────────────────────────────────────────────────

def _db_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return "sqlite:///" + os.path.join(BASE_DIR, "drkelley.db")
    # Railway hands out postgres:// / postgresql:// — pin the psycopg3 driver.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


engine = create_engine(_db_url(), pool_pre_ping=True, future=True)

if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def _sqlite_fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

meta = MetaData()

chemicals = Table(
    "chemicals", meta,
    Column("id", Integer, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("rei_hours", Float, nullable=False, default=0),
)

afflictions = Table(
    "afflictions", meta,
    Column("id", Integer, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("created_at", DateTime, server_default=func.now()),
)

templates = Table(
    "treatment_templates", meta,
    Column("id", Integer, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("method", Text, nullable=False),
    Column("chemical_id", Integer, ForeignKey("chemicals.id", ondelete="RESTRICT"), nullable=False),
    Column("rate", Text, nullable=False, default=""),
    Column("app_week", Text, nullable=False),          # ISO "2026-W40"
    Column("created_at", DateTime, server_default=func.now()),
)

plan_steps = Table(
    "plan_steps", meta,
    Column("id", Integer, primary_key=True),
    Column("affliction_id", Integer, ForeignKey("afflictions.id", ondelete="CASCADE"), nullable=False),
    Column("template_id", Integer, ForeignKey("treatment_templates.id", ondelete="CASCADE"), nullable=False),
    Column("position", Integer, nullable=False),
    Column("frequency", Text, nullable=False, default=""),
    Column("wait", Text, nullable=False, default=""),  # wait since the previous step
)

def init_db():
    meta.create_all(engine)
    with engine.begin() as conn:
        existing = {n.lower() for n in conn.scalars(select(chemicals.c.name))}
        with open(REI_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = row["Pesticide"].strip()
                if name and name.lower() not in existing:
                    conn.execute(insert(chemicals).values(
                        name=name, rei_hours=float(row["REI"] or 0)))
    engine.dispose()   # don't hand pooled connections to forked gunicorn workers


init_db()


# ── Helpers ───────────────────────────────────────────────────────────────────

def week_key(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_label(key):
    try:
        y, w = key.split("-W")
        return f"Week {int(w)}, {y}"
    except (ValueError, AttributeError):
        return key or ""


def week_options(include=None):
    today = date.today()
    keys, seen = [], set()
    for i in range(-WEEKS_BACK, WEEKS_AHEAD + 1):
        k = week_key(today + timedelta(weeks=i))
        if k not in seen:
            seen.add(k)
            keys.append(k)
    if include and include not in seen:
        keys.insert(0, include)
    return [(k, week_label(k)) for k in keys]


def fmt_rei(h):
    return f"{h:g}h"


app.jinja_env.filters["week_label"] = week_label
app.jinja_env.filters["rei"] = fmt_rei


def _template_rows(conn):
    q = (select(templates, chemicals.c.name.label("chemical"), chemicals.c.rei_hours)
         .join(chemicals, templates.c.chemical_id == chemicals.c.id)
         .order_by(func.lower(templates.c.name)))
    return conn.execute(q).mappings().all()


def _steps_for(conn, affliction_id):
    q = (select(plan_steps, templates.c.name.label("template"), templates.c.method,
                templates.c.rate, templates.c.app_week,
                chemicals.c.name.label("chemical"), chemicals.c.rei_hours)
         .join(templates, plan_steps.c.template_id == templates.c.id)
         .join(chemicals, templates.c.chemical_id == chemicals.c.id)
         .where(plan_steps.c.affliction_id == affliction_id)
         .order_by(plan_steps.c.position, plan_steps.c.id))
    return conn.execute(q).mappings().all()


def _renumber(conn, affliction_id):
    ids = conn.scalars(select(plan_steps.c.id)
                       .where(plan_steps.c.affliction_id == affliction_id)
                       .order_by(plan_steps.c.position, plan_steps.c.id)).all()
    for pos, sid in enumerate(ids, 1):
        conn.execute(update(plan_steps).where(plan_steps.c.id == sid).values(position=pos))
    return ids


def _save_step_fields(conn, affliction_id, form):
    """Every submit from the plan form saves its frequency/wait boxes first."""
    for sid in conn.scalars(select(plan_steps.c.id)
                            .where(plan_steps.c.affliction_id == affliction_id)):
        vals = {}
        if f"freq_{sid}" in form:
            vals["frequency"] = form[f"freq_{sid}"].strip()
        if f"wait_{sid}" in form:
            vals["wait"] = form[f"wait_{sid}"].strip()
        if vals:
            conn.execute(update(plan_steps).where(plan_steps.c.id == sid).values(**vals))


def _home(aid=None, **kw):
    return redirect(url_for("index", a=aid, **kw))


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    aid = request.args.get("a", type=int)
    edit_id = request.args.get("edit", type=int)
    with engine.begin() as conn:
        step_counts = dict(conn.execute(
            select(plan_steps.c.affliction_id, func.count())
            .group_by(plan_steps.c.affliction_id)).all())
        aff_list = [dict(r, n_steps=step_counts.get(r["id"], 0)) for r in conn.execute(
            select(afflictions).order_by(func.lower(afflictions.c.name))).mappings()]
        selected = next((a for a in aff_list if a["id"] == aid), None)
        steps = _steps_for(conn, aid) if selected else []

        tmpl_list = _template_rows(conn)
        usage = dict(conn.execute(
            select(plan_steps.c.template_id, func.count(func.distinct(plan_steps.c.affliction_id)))
            .group_by(plan_steps.c.template_id)).all())
        editing = next((t for t in tmpl_list if t["id"] == edit_id), None)
        chem_list = conn.execute(select(chemicals).order_by(func.lower(chemicals.c.name))).mappings().all()

    return render_template(
        "index.html",
        afflictions=aff_list, selected=selected, steps=steps,
        templates=tmpl_list, usage=usage, editing=editing,
        chemicals=chem_list, methods=METHODS,
        weeks=week_options(editing["app_week"] if editing else None),
        this_week=week_key(date.today()),
    )


# ── Afflictions ───────────────────────────────────────────────────────────────

@app.post("/afflictions")
def add_affliction():
    name = request.form.get("name", "").strip()
    aid = request.form.get("current", type=int)
    if not name:
        flash("An affliction needs a name.")
        return _home(aid)
    with engine.begin() as conn:
        dup = conn.scalar(select(afflictions.c.id)
                          .where(func.lower(afflictions.c.name) == name.lower()))
        if dup:
            flash(f"“{name}” already exists.")
            return _home(dup)
        aid = conn.execute(insert(afflictions).values(name=name)
                           .returning(afflictions.c.id)).scalar()
    flash(f"Added affliction: {name}")
    return _home(aid)


@app.post("/afflictions/<int:aid>/rename")
def rename_affliction(aid):
    name = request.form.get("name", "").strip()
    if not name:
        flash("Name can't be blank.")
        return _home(aid)
    with engine.begin() as conn:
        dup = conn.scalar(select(afflictions.c.id).where(
            func.lower(afflictions.c.name) == name.lower(), afflictions.c.id != aid))
        if dup:
            flash(f"“{name}” already exists.")
            return _home(aid)
        conn.execute(update(afflictions).where(afflictions.c.id == aid).values(name=name))
    return _home(aid)


@app.post("/afflictions/<int:aid>/delete")
def delete_affliction(aid):
    with engine.begin() as conn:
        conn.execute(delete(plan_steps).where(plan_steps.c.affliction_id == aid))
        conn.execute(delete(afflictions).where(afflictions.c.id == aid))
    flash("Affliction deleted.")
    return _home()


# ── Treatment plan steps ──────────────────────────────────────────────────────

@app.post("/afflictions/<int:aid>/apply/<int:tid>")
def apply_template(aid, tid):
    with engine.begin() as conn:
        if not conn.scalar(select(afflictions.c.id).where(afflictions.c.id == aid)):
            flash("Pick an affliction on the left first.")
            return _home()
        last = conn.scalar(select(func.max(plan_steps.c.position))
                           .where(plan_steps.c.affliction_id == aid)) or 0
        conn.execute(insert(plan_steps).values(
            affliction_id=aid, template_id=tid, position=last + 1,
            frequency="", wait=""))
        tname = conn.scalar(select(templates.c.name).where(templates.c.id == tid))
    flash(f"Added “{tname}” as treatment #{last + 1}.")
    return _home(aid)


@app.post("/afflictions/<int:aid>/steps/save")
def save_steps(aid):
    with engine.begin() as conn:
        _save_step_fields(conn, aid, request.form)
    flash("Treatment plan saved.")
    return _home(aid)


@app.post("/afflictions/<int:aid>/steps/<int:sid>/move")
def move_step(aid, sid):
    direction = request.args.get("dir")
    with engine.begin() as conn:
        _save_step_fields(conn, aid, request.form)
        ids = _renumber(conn, aid)
        if sid in ids:
            i = ids.index(sid)
            j = i - 1 if direction == "up" else i + 1
            if 0 <= j < len(ids):
                conn.execute(update(plan_steps).where(plan_steps.c.id == ids[i]).values(position=j + 1))
                conn.execute(update(plan_steps).where(plan_steps.c.id == ids[j]).values(position=i + 1))
    return _home(aid)


@app.post("/afflictions/<int:aid>/steps/<int:sid>/delete")
def delete_step(aid, sid):
    with engine.begin() as conn:
        _save_step_fields(conn, aid, request.form)
        conn.execute(delete(plan_steps).where(plan_steps.c.id == sid,
                                              plan_steps.c.affliction_id == aid))
        _renumber(conn, aid)
    return _home(aid)


# ── Treatment templates ───────────────────────────────────────────────────────

@app.post("/templates")
def save_template():
    f = request.form
    aid = f.get("current", type=int)
    tid = f.get("id", type=int)
    vals = {
        "name": f.get("name", "").strip(),
        "method": f.get("method", ""),
        "chemical_id": f.get("chemical_id", type=int),
        "rate": f.get("rate", "").strip(),
        "app_week": f.get("app_week", ""),
    }
    problems = []
    if not vals["name"]:
        problems.append("a name")
    if vals["method"] not in METHODS:
        problems.append("a method")
    if not vals["chemical_id"]:
        problems.append("a chemical")
    if not vals["rate"]:
        problems.append("a rate")
    if not vals["app_week"]:
        problems.append("a week of application")
    if problems:
        flash("Template still needs " + ", ".join(problems) + ".")
        return _home(aid, edit=tid)

    with engine.begin() as conn:
        dup = select(templates.c.id).where(func.lower(templates.c.name) == vals["name"].lower())
        if tid:
            dup = dup.where(templates.c.id != tid)
        if conn.scalar(dup):
            flash(f"A template named “{vals['name']}” already exists — names must be unique.")
            return _home(aid, edit=tid)
        if tid:
            conn.execute(update(templates).where(templates.c.id == tid).values(**vals))
            flash(f"Template “{vals['name']}” updated.")
        else:
            conn.execute(insert(templates).values(**vals))
            flash(f"Template “{vals['name']}” saved.")
    return _home(aid)


@app.post("/templates/<int:tid>/delete")
def delete_template(tid):
    aid = request.form.get("current", type=int)
    with engine.begin() as conn:
        affected = conn.scalars(select(func.distinct(plan_steps.c.affliction_id))
                                .where(plan_steps.c.template_id == tid)).all()
        conn.execute(delete(plan_steps).where(plan_steps.c.template_id == tid))
        conn.execute(delete(templates).where(templates.c.id == tid))
        for a in affected:
            _renumber(conn, a)
    flash("Template deleted.")
    return _home(aid)


# ── Chemicals / REI list ────────────────────────────────────────────

@app.get("/pharmacy")
def pharmacy():
    with engine.begin() as conn:
        rows = conn.execute(select(chemicals).order_by(func.lower(chemicals.c.name))).mappings().all()
        used = set(conn.scalars(select(func.distinct(templates.c.chemical_id))))
    return render_template("pharmacy.html", chemicals=rows, used=used)


@app.post("/pharmacy")
def pharmacy_save():
    name = request.form.get("name", "").strip()
    try:
        rei = float(request.form.get("rei", ""))
    except ValueError:
        flash("REI must be a number of hours.")
        return redirect(url_for("pharmacy"))
    if not name:
        flash("Chemical needs a name.")
        return redirect(url_for("pharmacy"))
    with engine.begin() as conn:
        cid = conn.scalar(select(chemicals.c.id).where(func.lower(chemicals.c.name) == name.lower()))
        if cid:
            conn.execute(update(chemicals).where(chemicals.c.id == cid).values(rei_hours=rei))
            flash(f"{name}: REI set to {rei:g}h.")
        else:
            conn.execute(insert(chemicals).values(name=name, rei_hours=rei))
            flash(f"Added {name} ({rei:g}h REI).")
    return redirect(url_for("pharmacy"))


@app.post("/pharmacy/<int:cid>/delete")
def pharmacy_delete(cid):
    with engine.begin() as conn:
        if conn.scalar(select(templates.c.id).where(templates.c.chemical_id == cid)):
            flash("That chemical is used by a template — delete the template first.")
        else:
            conn.execute(delete(chemicals).where(chemicals.c.id == cid))
            flash("Chemical removed.")
    return redirect(url_for("pharmacy"))


# ── CSV exports ───────────────────────────────────────────────────────────────

def _csv_response(rows, filename):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


def plans_table(conn):
    """Affliction | Treatment 1 | Frequency 1 | Treatment 2 | Wait 1→2 | Frequency 2 | …"""
    rows, width = [], 1
    for a in conn.execute(select(afflictions).order_by(func.lower(afflictions.c.name))).mappings():
        row = [a["name"]]
        for i, s in enumerate(_steps_for(conn, a["id"])):
            row.append(s["template"])
            if i > 0:
                row.append(s["wait"])
            row.append(s["frequency"])
        width = max(width, len(row))
        rows.append(row)

    n_steps = width // 3   # 1 + 2 cols for step 1 + 3 for each step after = 3n
    header = ["Affliction"]
    for n in range(1, n_steps + 1):
        header.append(f"Treatment {n}")
        if n > 1:
            header.append(f"Wait {n - 1}-{n}")
        header.append(f"Frequency {n}")
    return [header] + [r + [""] * (len(header) - len(r)) for r in rows]


@app.get("/export/plans.csv")
def export_plans():
    with engine.begin() as conn:
        return _csv_response(plans_table(conn), "dr_kelley_treatment_plans.csv")


@app.get("/export/templates.csv")
def export_templates():
    with engine.begin() as conn:
        rows = [["Template", "Method", "Chemical", "Rate", "Week of application", "REI (hours)"]]
        rows += [[t["name"], t["method"], t["chemical"], t["rate"], t["app_week"], f"{t['rei_hours']:g}"]
                 for t in _template_rows(conn)]
    return _csv_response(rows, "dr_kelley_treatment_templates.csv")


@app.get("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
