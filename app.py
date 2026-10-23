
from flask import Flask, render_template, request, redirect, url_for, flash, session, abort
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime, date, timedelta, timezone
from uuid import uuid4
import json
import os
import secrets
import ssl
import csv
import io
import subprocess
import shutil
import sqlite3
from pathlib import Path
from flask import Response
from sqlalchemy import inspect, text
from flask_wtf.csrf import CSRFProtect
from sqlalchemy.exc import IntegrityError
from werkzeug.middleware.proxy_fix import ProxyFix

app=Flask(__name__)
app.config["SECRET_KEY"]=os.getenv("SECRET_KEY") or ("local-development-only-secret" if os.getenv("FLASK_ENV")=="development" else None)
if not app.config["SECRET_KEY"]: raise RuntimeError("SECRET_KEY environment variable must be configured")
app.config["SESSION_COOKIE_HTTPONLY"]=True
app.config["SESSION_COOKIE_SAMESITE"]="Lax"
app.config["SESSION_COOKIE_SECURE"]=os.getenv("COOKIE_SECURE","1") == "1"
app.config["PERMANENT_SESSION_LIFETIME"]=__import__("datetime").timedelta(hours=12)
app.config["WTF_CSRF_TIME_LIMIT"]=3600
app.wsgi_app=ProxyFix(app.wsgi_app,x_proto=1,x_host=1)
database_url=os.getenv("DATABASE_URL")
if os.getenv('D1_WORKER_URL'):
    from sqlalchemy.engine import URL
    if not os.getenv('D1_WORKER_SECRET'): raise RuntimeError('D1_WORKER_SECRET is required')
    database_url=URL.create('cloudflare_d1',username='7bcbe6b2e68e329b6de8d0005b0c96dd',password=os.environ['D1_WORKER_SECRET'],host='4680a988-a5c6-424b-aee6-46e8a80596d4')
    app.config['SQLALCHEMY_ENGINE_OPTIONS']={'connect_args': {'base_url':os.environ['D1_WORKER_URL'].rstrip('/')}}
if isinstance(database_url,str) and database_url.startswith("postgres://"):
    database_url="postgresql://"+database_url[len("postgres://"):]
app.config["SQLALCHEMY_DATABASE_URI"]=database_url or "sqlite:///orderflow.db"
if os.getenv("VERCEL") and not database_url:
    raise RuntimeError("Configure the production database before deploying; local SQLite is not persistent on Vercel.")
if os.getenv("RAILWAY_ENVIRONMENT") and not database_url:
    raise RuntimeError("DATABASE_URL required on Railway. Attach a PostgreSQL database.")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"]=False
db=SQLAlchemy(app)
csrf=CSRFProtect(app)

class User(db.Model):
    id=db.Column(db.Integer,primary_key=True)
    username=db.Column(db.String(80),unique=True,nullable=False)
    password_hash=db.Column(db.String(255),nullable=False)
    name=db.Column(db.String(120),nullable=False)
    role=db.Column(db.String(30),default="Admin")

class Customer(db.Model):
    id=db.Column(db.Integer,primary_key=True)
    name=db.Column(db.String(150),nullable=False)
    phone=db.Column(db.String(30))
    gst=db.Column(db.String(30))
    address=db.Column(db.Text)

class Order(db.Model):
    id=db.Column(db.Integer,primary_key=True)
    order_no=db.Column(db.String(50),unique=True,nullable=False)
    customer_id=db.Column(db.Integer,db.ForeignKey("customer.id"),nullable=False)
    product_name=db.Column(db.String(180),nullable=False)
    quantity=db.Column(db.Integer,nullable=False)
    rate=db.Column(db.Float,nullable=False,default=0) # Legacy compatibility; order form never uses money
    order_date=db.Column(db.Date,default=date.today)
    expected_date=db.Column(db.Date)
    production_status=db.Column(db.String(30),default="Processing")
    transfer_status=db.Column(db.String(30),default="Not Sent")
    sale_status=db.Column(db.String(30),default="Not Dispatched")
    notes=db.Column(db.Text)
    calendar_event_id=db.Column(db.String(255),nullable=True)
    produced_qty=db.Column(db.Integer,nullable=False,default=0)
    sent_qty=db.Column(db.Integer,nullable=False,default=0)
    received_qty=db.Column(db.Integer,nullable=False,default=0)
    dispatched_qty=db.Column(db.Integer,nullable=False,default=0)
    cancelled_at=db.Column(db.DateTime,nullable=True)
    calendar_sync_error=db.Column(db.Text,nullable=True)
    customer=db.relationship("Customer")
    @property
    def remaining_production(self): return max(0,self.quantity-(self.produced_qty or 0))
    @property
    def remaining_dispatch(self): return max(0,self.quantity-(self.dispatched_qty or 0))
    @property
    def available_to_send(self): return max(0,(self.produced_qty or 0)-(self.sent_qty or 0))
    @property
    def in_transit(self): return max(0,(self.sent_qty or 0)-(self.received_qty or 0))
    @property
    def available_to_dispatch(self): return max(0,(self.received_qty or 0)-(self.dispatched_qty or 0))
    @property
    def days_left(self):
        return (self.expected_date-date.today()).days if self.expected_date else None
    @property
    def priority(self):
        if self.cancelled_at: return "cancelled"
        if self.remaining_dispatch==0: return "done"
        if self.days_left is None: return "no-date"
        if self.days_left<0: return "overdue"
        if self.days_left<=2: return "urgent"
        if self.days_left==3: return "soon"
        return "normal"
    @property
    def total(self): return self.quantity*self.rate

class Movement(db.Model):
    id=db.Column(db.Integer,primary_key=True)
    order_id=db.Column(db.Integer,db.ForeignKey("order.id"),nullable=False,index=True)
    action=db.Column(db.String(40),nullable=False)
    quantity=db.Column(db.Integer,nullable=False)
    username=db.Column(db.String(80),nullable=False)
    created_at=db.Column(db.DateTime,default=datetime.utcnow)
    order=db.relationship("Order")

class ReminderLog(db.Model):
    id=db.Column(db.Integer,primary_key=True)
    order_id=db.Column(db.Integer,db.ForeignKey("order.id"),nullable=False,index=True)
    reminder_date=db.Column(db.Date,nullable=False)
    days_left=db.Column(db.Integer,nullable=False)
    channel=db.Column(db.String(30),nullable=False)
    status=db.Column(db.String(30),nullable=False)
    __table_args__=(db.UniqueConstraint("order_id","reminder_date","channel",name="uq_order_reminder_day"),)

class Payment(db.Model):
    id=db.Column(db.Integer,primary_key=True)
    order_id=db.Column(db.Integer,db.ForeignKey("order.id"),unique=True,nullable=False)
    amount_due=db.Column(db.Float,default=0)
    amount_paid=db.Column(db.Float,default=0)
    due_date=db.Column(db.Date)
    status=db.Column(db.String(30),default="Unpaid")
    last_reminder=db.Column(db.DateTime)
    order=db.relationship("Order")
    @property
    def balance(self): return max(0,(self.amount_due or 0)-(self.amount_paid or 0))
    @property
    def overdue_days(self):
        if self.balance>0 and self.due_date and date.today()>self.due_date:
            return (date.today()-self.due_date).days
        return 0

class CalendarConnection(db.Model):
    id=db.Column(db.Integer,primary_key=True)
    provider=db.Column(db.String(30),default="google",unique=True)
    credentials_json=db.Column(db.Text,nullable=False)

class Audit(db.Model):
    id=db.Column(db.Integer,primary_key=True)
    username=db.Column(db.String(80))
    action=db.Column(db.String(255))
    created_at=db.Column(db.DateTime,default=datetime.utcnow)

def permitted(*allowed):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args,**kwargs):
            if "uid" not in session: return redirect(url_for("login"))
            if session.get("role") not in allowed and session.get("role")!="Admin": abort(403)
            return fn(*args,**kwargs)
        return wrapped
    return decorator

def logged(fn):
    @wraps(fn)
    def w(*a,**k):
        if "uid" not in session:return redirect(url_for("login"))
        return fn(*a,**k)
    return w

def log(x):
    db.session.add(Audit(username=session.get("username","system"),action=x)); db.session.commit()

@app.route("/login",methods=["GET","POST"])
def login():
    if request.method=="POST":
        u=User.query.filter_by(username=request.form["username"]).first()
        if u and check_password_hash(u.password_hash,request.form["password"]):
            session.clear();session.permanent=True
            session.update(uid=u.id,username=u.username,name=u.name,role=u.role)
            return redirect("/")
        flash("Invalid login")
    return render_template("login.html")

@app.post("/logout")
def logout(): session.clear(); return redirect("/login")

@app.route("/")
@logged
def dashboard():
    orders=Order.query.order_by(Order.id.desc()).all()
    active=sorted((o for o in orders if not o.cancelled_at and o.remaining_dispatch>0),key=lambda o:(o.days_left is None,o.days_left if o.days_left is not None else 100000,o.id))
    payments=Payment.query.all()
    return render_template("dashboard.html",orders=orders,active=active,
        urgent=[o for o in active if o.priority in ("overdue","urgent","soon")],
        processing=Order.query.filter_by(production_status="Processing").count(),
        ready=Order.query.filter_by(production_status="Ready").count(),
        pending_pay=sum(p.balance for p in payments) if session.get("role") in ("Admin","Accounts") else None,
        overdue=sum(1 for p in payments if p.overdue_days>0) if session.get("role") in ("Admin","Accounts") else None)

@app.route("/customers",methods=["GET","POST"])
@permitted("Selling")
def customers():
    if request.method=="POST":
        c=Customer(name=request.form["name"],phone=request.form.get("phone"),gst=request.form.get("gst"),address=request.form.get("address"))
        db.session.add(c);db.session.commit();log("Customer added: "+c.name)
        return redirect("/customers")
    return render_template("customers.html",customers=Customer.query.all())

def sync_calendar_event(o):
    """Best-effort sync; a failed remote request never prevents saving an order."""
    if not o.expected_date: return
    connection=CalendarConnection.query.filter_by(provider="google").first()
    if not connection: return
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        data=json.loads(connection.credentials_json)
        creds=Credentials.from_authorized_user_info(data,scopes=["https://www.googleapis.com/auth/calendar.events"])
        if not creds.valid:
            if creds.expired and creds.refresh_token: creds.refresh(Request())
            else: raise RuntimeError("Google Calendar requires reconnection")
        connection.credentials_json=creds.to_json()
        client=build("calendar","v3",credentials=creds,cache_discovery=False)
        title=f"Deliver order {o.order_no} — {o.customer.name}"
        body={"summary":title,"description":f"Product: {o.product_name}\nQuantity: {o.quantity}\nNotes: {o.notes or ''}",
              "start":{"date":o.expected_date.isoformat()},"end":{"date":(o.expected_date+timedelta(days=1)).isoformat()},
              "reminders":{"useDefault":False,"overrides":[{"method":"popup","minutes":3*1440},{"method":"popup","minutes":2*1440},{"method":"popup","minutes":1440}]}}
        if o.calendar_event_id:
            try: client.events().update(calendarId="primary",eventId=o.calendar_event_id,body=body).execute()
            except Exception as e:
                if getattr(getattr(e,"resp",None),"status",None) == 404:
                    created=client.events().insert(calendarId="primary",body=body).execute();o.calendar_event_id=created["id"]
                else: raise
        else:
            created=client.events().insert(calendarId="primary",body=body).execute();o.calendar_event_id=created["id"]
        o.calendar_sync_error=None
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        o.calendar_sync_error=str(exc)[:500]
        db.session.commit()
        app.logger.exception("Calendar sync failed for %s",o.order_no)
        flash("Order saved, but Google Calendar sync failed. Check connection and retry from Orders.")

@app.route("/orders",methods=["GET","POST"])
@permitted("Selling")
def orders():
    if request.method=="POST":
        try:
            quantity=int(request.form["quantity"])
            expected=datetime.strptime(request.form["expected_date"],"%Y-%m-%d").date()
            customer= db.session.get(Customer,int(request.form["customer_id"]))
            product=request.form["product_name"].strip()
        except (ValueError, KeyError, TypeError):
            flash("Please enter a valid customer, product, quantity and delivery date.");return redirect("/orders")
        if not customer or not product or quantity<1:
            flash("Choose a customer and enter a product and positive quantity.");return redirect("/orders")
        o=Order(order_no="ORD-"+uuid4().hex[:12].upper(),customer_id=customer.id,
          product_name=product,quantity=quantity,rate=0,expected_date=expected,notes=request.form.get("notes",""))
        db.session.add(o);db.session.commit();log("Order created: "+o.order_no)
        sync_calendar_event(o)
        flash("Order added. No payment record was created.")
        return redirect("/orders")
    return render_template("orders.html",orders=Order.query.order_by(Order.id.desc()).all(),customers=Customer.query.all(),calendar_connected=CalendarConnection.query.first() is not None)

@app.post("/orders/<int:id>/delivery-date")
@permitted("Selling")
def change_delivery(id):
    o=Order.query.get_or_404(id)
    try: o.expected_date=datetime.strptime(request.form["expected_date"],"%Y-%m-%d").date()
    except (ValueError, KeyError): flash("Invalid date");return redirect("/orders")
    db.session.commit();log(f"{o.order_no} delivery date changed to {o.expected_date}")
    sync_calendar_event(o)
    return redirect("/orders")

@app.post("/orders/<int:id>/sync")
@permitted("Admin","Selling")
def retry_calendar(id):
    sync_calendar_event(Order.query.get_or_404(id))
    return redirect("/orders")

@app.route("/calendar/connect")
@permitted("Admin")
def calendar_connect():
    from google_auth_oauthlib.flow import Flow
    client_id=os.getenv("GOOGLE_CLIENT_ID");secret=os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not secret:
        flash("Configure GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first.");return redirect("/orders")
    config={"web":{"client_id":client_id,"client_secret":secret,"auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token"}}
    flow=Flow.from_client_config(config,scopes=["https://www.googleapis.com/auth/calendar.events"],redirect_uri=url_for("calendar_callback",_external=True,_scheme="https"))
    auth_url,state=flow.authorization_url(access_type="offline",include_granted_scopes="true",prompt="consent")
    session["google_oauth_state"]=state
    return redirect(auth_url)

@app.route("/calendar/callback")
@permitted("Admin")
def calendar_callback():
    from google_auth_oauthlib.flow import Flow
    state=session.pop("google_oauth_state",None)
    if not state or request.args.get("state")!=state: abort(400)
    config={"web":{"client_id":os.environ["GOOGLE_CLIENT_ID"],"client_secret":os.environ["GOOGLE_CLIENT_SECRET"],"auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token"}}
    flow=Flow.from_client_config(config,scopes=["https://www.googleapis.com/auth/calendar.events"],redirect_uri=url_for("calendar_callback",_external=True,_scheme="https"),state=state)
    flow.fetch_token(authorization_response=request.url)
    row=CalendarConnection.query.filter_by(provider="google").first()
    if not row: row=CalendarConnection(provider="google");db.session.add(row)
    row.credentials_json=flow.credentials.to_json();db.session.commit()
    flash("Google Calendar connected. New orders will sync automatically; sync existing orders from Orders.")
    return redirect("/orders")

def record_movement(o,action,qty):
    db.session.add(Movement(order_id=o.id,action=action,quantity=qty,username=session.get("username","system")))

def positive_qty(form):
    try:
        n=int(form.get("quantity", ""))
        if n<=0: raise ValueError()
        return n
    except (ValueError,TypeError):
        return None

def d1_movement(oid, action, qty):
    """Apply quantity and history together, validating stock inside the database."""
    import httpx
    field, limit, status = {
        'Produced': ('produced_qty','quantity','production_status'),
        'Sent': ('sent_qty','produced_qty','transfer_status'),
        'Received': ('received_qty','sent_qty','transfer_status'),
        'Dispatched': ('dispatched_qty','received_qty','sale_status'),
    }[action]
    status_sql = {
        'Produced': "CASE WHEN produced_qty+?=quantity THEN 'Ready' ELSE 'Processing' END",
        'Sent': "CASE WHEN ? > 0 THEN 'Sent' ELSE 'Sent' END",
        'Received': "CASE WHEN received_qty+?=sent_qty THEN 'Received' ELSE 'Sent' END",
        'Dispatched': "CASE WHEN dispatched_qty+?=quantity THEN 'Dispatched' ELSE 'Part Dispatched' END",
    }[action]
    statements=[
        {'sql':f'UPDATE "order" SET {field}={field}+?, {status}={status_sql} WHERE id=? AND cancelled_at IS NULL AND {field}+? <= {limit}', 'params':[qty,qty,oid,qty]},
        {'sql':"INSERT INTO movement (order_id,action,quantity,username,created_at) SELECT ?,?,?,?,? WHERE changes()=1",'params':[oid,action,qty,session['username'],datetime.utcnow().isoformat(' ')]},
        {'sql':"INSERT INTO audit (username,action,created_at) SELECT ?,?,? WHERE changes()=1",'params':[session['username'],f'Order {oid}: {action} {qty}',datetime.utcnow().isoformat(' ')]},
    ]
    response=httpx.post(os.environ['D1_WORKER_URL'].rstrip('/')+'/batch',headers={'Authorization':'Bearer '+os.environ['D1_WORKER_SECRET']},json={'statements':statements},timeout=30)
    response.raise_for_status()
    result=response.json()
    if not result.get('success'): raise RuntimeError('Stock update failed')
    db.session.expire_all()
    return result['result'][0]['meta']['changes']==1

@app.post("/production/<int:id>")
@permitted("Production")
def production(id):
    o=Order.query.get_or_404(id);n=positive_qty(request.form)
    if os.getenv('D1_WORKER_URL'):
        if n is None or not d1_movement(id,'Produced',n): flash('Invalid quantity or cancelled order.')
        return redirect('/production')
    if o.cancelled_at or n is None or n>o.remaining_production:
        flash("Invalid quantity, or this order has been cancelled.")
    else:
        o.produced_qty+=n
        o.production_status="Ready" if o.remaining_production==0 else "Processing"
        record_movement(o,"Produced",n);db.session.commit();log(f"{o.order_no}: produced {n}")
    return redirect("/production")

@app.route("/production")
@logged
def production_page():return render_template("production.html",orders=Order.query.order_by(Order.id.desc()).all())

@app.post("/transfer/<int:id>")
@permitted("Production","Selling")
def transfer(id):
    o=Order.query.get_or_404(id); n=positive_qty(request.form);action=request.form.get("action")
    if os.getenv('D1_WORKER_URL'):
        allowed=(action=='send' and session['role'] in ('Admin','Production')) or (action=='receive' and session['role'] in ('Admin','Selling'))
        if not allowed or n is None or not d1_movement(id,'Sent' if action=='send' else 'Received',n): flash('Invalid quantity or insufficient permission.')
        return redirect('/transfers')
    if o.cancelled_at or n is None:
        flash("Invalid transfer quantity or cancelled order.")
    elif action=="send" and session.get("role") in ("Admin","Production") and n<=o.available_to_send:
        o.sent_qty+=n;o.transfer_status="Sent";record_movement(o,"Sent",n);db.session.commit();log(f"{o.order_no}: sent {n}")
    elif action=="receive" and session.get("role") in ("Admin","Selling") and n<=o.in_transit:
        o.received_qty+=n;o.transfer_status="Received" if o.in_transit==0 else "Sent"
        record_movement(o,"Received",n);db.session.commit();log(f"{o.order_no}: received {n}")
    else:flash("Not enough available quantity, or insufficient permission.")
    return redirect("/transfers")

@app.route("/transfers")
@logged
def transfers():return render_template("transfers.html",orders=Order.query.order_by(Order.id.desc()).all())

@app.post("/dispatch/<int:id>")
@permitted("Selling")
def dispatch(id):
    o=Order.query.get_or_404(id);n=positive_qty(request.form)
    if os.getenv('D1_WORKER_URL'):
        if n is None or not d1_movement(id,'Dispatched',n): flash('Quantity exceeds available stock or order cancelled.')
        elif o.remaining_dispatch==0: remove_calendar_event(o)
        return redirect('/dispatch')
    if o.cancelled_at or n is None or n>o.available_to_dispatch:
        flash("Quantity exceeds stock received by selling, or cancelled order.")
    else:
        o.dispatched_qty+=n;o.sale_status="Dispatched" if o.remaining_dispatch==0 else "Part Dispatched"
        record_movement(o,"Dispatched",n);db.session.commit();log(f"{o.order_no}: dispatched {n}")
        if o.remaining_dispatch==0: remove_calendar_event(o)
    return redirect("/dispatch")

@app.route("/dispatch")
@logged
def dispatch_page():return render_template("dispatch.html",orders=Order.query.order_by(Order.id.desc()).all())

@app.route("/work-list")
@logged
def work_list():
    active=[o for o in Order.query.all() if not o.cancelled_at and o.remaining_dispatch>0]
    active.sort(key=lambda o:(o.expected_date is None,o.expected_date or date.max,o.id))
    return render_template("work_list.html",orders=active)

@app.post("/orders/<int:id>/edit")
@permitted("Selling")
def edit_order(id):
    o=Order.query.get_or_404(id)
    if o.cancelled_at: flash("Cancelled orders cannot be edited.");return redirect('/orders')
    try:
        quantity=int(request.form['quantity']);customer_id=int(request.form['customer_id'])
        expected=datetime.strptime(request.form['expected_date'],'%Y-%m-%d').date()
        product=request.form['product_name'].strip()
    except (ValueError,KeyError,TypeError):
        flash('Invalid order details.');return redirect('/orders')
    min_qty=max(o.produced_qty,o.sent_qty,o.received_qty,o.dispatched_qty)
    if quantity<1 or quantity<min_qty or not product or not db.session.get(Customer,customer_id):
        flash(f'Enter a valid order. Quantity cannot be less than already processed quantity ({min_qty}).');return redirect('/orders')
    o.quantity=quantity;o.customer_id=customer_id;o.product_name=product;o.expected_date=expected;o.notes=request.form.get('notes','')
    if o.remaining_production>0:o.production_status='Processing'
    db.session.commit();log(f'{o.order_no} details edited');sync_calendar_event(o)
    flash('Order updated.');return redirect('/orders')

@app.post("/orders/<int:id>/cancel")
@permitted("Selling")
def cancel_order(id):
    o=Order.query.get_or_404(id)
    if any([o.produced_qty,o.sent_qty,o.received_qty,o.dispatched_qty]):
        flash('Cannot cancel an order after production/transfers/dispatch. Contact administrator for reconciliation.');return redirect('/orders')
    o.cancelled_at=datetime.utcnow();db.session.commit();log(f'{o.order_no} cancelled');remove_calendar_event(o)
    flash('Order cancelled; history retained.');return redirect('/orders')

def google_client():
    conn=CalendarConnection.query.filter_by(provider='google').first()
    if not conn:return None
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    creds=Credentials.from_authorized_user_info(json.loads(conn.credentials_json),scopes=['https://www.googleapis.com/auth/calendar.events'])
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request());conn.credentials_json=creds.to_json();db.session.commit()
    return build('calendar','v3',credentials=creds,cache_discovery=False)

def remove_calendar_event(o):
    if not o.calendar_event_id:return
    try:
        service=google_client()
        if service:service.events().delete(calendarId='primary',eventId=o.calendar_event_id).execute()
        o.calendar_event_id=None;o.calendar_sync_error=None;db.session.commit()
    except Exception as exc:
        db.session.rollback();app.logger.exception('Calendar event delete failed for %s',o.order_no)
        o.calendar_sync_error=str(exc)[:500];db.session.commit()

@app.route("/payments",methods=["GET"])
@permitted("Accounts")
def payments():
    return render_template("payments.html",payments=Payment.query.order_by(Payment.id.desc()).all(),
        unlinked_orders=Order.query.filter(~Order.id.in_(db.session.query(Payment.order_id))).order_by(Order.id.desc()).all())

@app.post("/payments/add")
@permitted("Accounts")
def payment_create():
    try:
        oid=int(request.form["order_id"])
        amount=float(request.form["amount_due"])
        date_str=request.form.get("due_date","")
        due=datetime.strptime(date_str,"%Y-%m-%d").date() if date_str else None
        if not 0<amount<100000000000: raise ValueError("amount")
    except (ValueError,TypeError,KeyError):
        flash("Enter a valid positive payment amount and due date.");return redirect("/payments")
    if not db.session.get(Order,oid) or Payment.query.filter_by(order_id=oid).first():
        flash("Select an order without a payment record.");return redirect("/payments")
    db.session.add(Payment(order_id=oid,amount_due=amount,amount_paid=0,due_date=due,status="Unpaid"))
    db.session.commit();log(f"Payment tracking added to order ID {oid}")
    return redirect("/payments")

@app.post("/payment/<int:id>")
@permitted("Accounts")
def payment_update(id):
    p=Payment.query.get_or_404(id)
    try: p.amount_paid=float(request.form.get("amount_paid") or 0)
    except ValueError:
        flash("Invalid received amount");return redirect("/payments")
    if not (0 <= p.amount_paid <= p.amount_due):
        flash("Received amount must be between zero and the order total."); return redirect("/payments")
    d=request.form.get("due_date")
    try:p.due_date=datetime.strptime(d,"%Y-%m-%d").date() if d else None
    except ValueError:flash("Invalid payment due date");return redirect("/payments")
    p.status="Paid" if p.amount_paid>=p.amount_due else ("Part Paid" if p.amount_paid>0 else "Unpaid")
    db.session.commit();log(f"{p.order.order_no} payment: {p.status}")
    return redirect("/payments")

@app.post("/reminder/<int:id>")
@permitted("Accounts")
def reminder(id):
    p=Payment.query.get_or_404(id);p.last_reminder=datetime.utcnow();db.session.commit()
    flash(f"Reminder recorded for {p.order.customer.name}. Connect WhatsApp/SMS later for automatic sending.")
    log(f"Payment reminder recorded: {p.order.order_no}")
    return redirect("/payments")

@app.route("/reports")
@permitted("Accounts")
def reports():
    ps=Payment.query.all()
    return render_template("reports.html",sales=sum(p.amount_due for p in ps),
      received=sum(p.amount_paid for p in ps),pending=sum(p.balance for p in ps),
      logs=Audit.query.order_by(Audit.id.desc()).limit(50).all())

@app.route("/users",methods=["GET","POST"])
@permitted("Admin")
def users():
    if request.method=="POST":
        if request.form.get("role") not in ("Admin","Production","Selling","Accounts") or len(request.form.get("password",""))<12:
            flash("Choose a valid role and a password at least 12 characters long.");return redirect("/users")
        u=User(name=request.form["name"],username=request.form["username"],role=request.form["role"],password_hash=generate_password_hash(request.form["password"]))
        db.session.add(u);db.session.commit();return redirect("/users")
    return render_template("users.html",users=User.query.all())

@app.get('/orders/export.csv')
@permitted('Selling')
def export_orders():
    stream=io.StringIO();writer=csv.writer(stream)
    writer.writerow(['Order No','Party','Product','Quantity','Promised Date','Produced','Sent','Received','Dispatched','Remaining','Cancelled'])
    for o in Order.query.order_by(Order.id.desc()).all():
        writer.writerow([o.order_no,o.customer.name,o.product_name,o.quantity,o.expected_date,o.produced_qty,o.sent_qty,o.received_qty,o.dispatched_qty,o.remaining_dispatch,bool(o.cancelled_at)])
    return Response(stream.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=orderflow-orders.csv'})

@app.cli.command('migrate-db')
def migrate_db():
    """Add v5 columns without deleting or recreating existing tables."""
    db.create_all()
    columns={c['name'] for c in inspect(db.engine).get_columns('order')}
    types={'calendar_event_id':'VARCHAR(255) NULL','produced_qty':'INTEGER NOT NULL DEFAULT 0','sent_qty':'INTEGER NOT NULL DEFAULT 0',
           'received_qty':'INTEGER NOT NULL DEFAULT 0','dispatched_qty':'INTEGER NOT NULL DEFAULT 0',
           'cancelled_at':'TIMESTAMP NULL','calendar_sync_error':'TEXT NULL'}
    with db.engine.begin() as con:
        for col,kind in types.items():
            if col not in columns:
                con.execute(text(f'ALTER TABLE "order" ADD COLUMN {col} {kind}'))
                print('Added',col)
        # Migrate old orders conservatively: do not invent quantity movements.
    print('Migration complete. Existing production/transfer statuses have not been converted to quantities; reconcile those manually.')

@app.cli.command('backup-db')
def backup_db():
    """Manual backup; schedule through a cron service or Railway cron job."""
    if os.getenv('D1_WORKER_URL'):
        raise RuntimeError('Use Cloudflare D1 Time Travel or wrangler d1 export for D1 backups.')
    from datetime import datetime as dt
    directory=Path(os.getenv('BACKUP_DIR','backups'));directory.mkdir(parents=True,exist_ok=True)
    target=directory / ('orderflow-'+dt.utcnow().strftime('%Y%m%d-%H%M%S'))
    url=app.config['SQLALCHEMY_DATABASE_URI']
    if url.startswith('sqlite:'):
        filename=db.engine.url.database
        if not filename:raise RuntimeError('SQLite database path missing')
        source=sqlite3.connect(filename);dest=sqlite3.connect(str(target)+'.sqlite3')
        with dest:source.backup(dest)
        dest.close();source.close();print('Backup created:',str(target)+'.sqlite3')
    else:
        if shutil.which('pg_dump') is None:raise RuntimeError('pg_dump is required for PostgreSQL backups')
        result=subprocess.run(['pg_dump','--format=custom','--file',str(target)+'.dump',os.environ['DATABASE_URL']],check=True)
        backup_path=str(target)+'.dump'
        print('Backup created:',backup_path)
    bucket=os.getenv('BACKUP_S3_BUCKET')
    if bucket:
        import boto3
        config={}
        if os.getenv('BACKUP_S3_ENDPOINT_URL'):config['endpoint_url']=os.environ['BACKUP_S3_ENDPOINT_URL']
        s3=boto3.client('s3',**config)
        key=os.getenv('BACKUP_S3_PREFIX','orderflow/')+Path(backup_path).name
        s3.upload_file(backup_path,bucket,key)
        print('Uploaded backup to remote object storage:',bucket,key)
    else:
        print('WARNING: Backup is LOCAL ONLY. Configure BACKUP_S3_BUCKET and S3 credentials for durable offsite backups.')

def send_delivery_reminders():
    """Send daily delivery alerts; record successful sends in the database."""
    import smtplib
    from email.message import EmailMessage
    today=date.today()
    orders=[o for o in Order.query.all() if not o.cancelled_at and o.remaining_dispatch>0 and o.days_left is not None and o.days_left<=3]
    orders.sort(key=lambda o:(o.days_left,o.id))
    outstanding=[]
    for o in orders:
        if not ReminderLog.query.filter_by(order_id=o.id,reminder_date=today,channel='email').first():
            outstanding.append(o)
    if not outstanding:
        return {"sent_orders": 0}
    host=os.getenv('SMTP_HOST','smtp.gmail.com');to=os.getenv('ALERT_EMAIL_TO');user=os.getenv('SMTP_USER');password=os.getenv('SMTP_PASSWORD')
    if not all([host,to,user,password]):
        raise RuntimeError('Configure SMTP_USER, SMTP_PASSWORD and ALERT_EMAIL_TO')
    msg=EmailMessage();msg['Subject']=f'OrderFlow: {len(outstanding)} delivery reminder(s) — {today.isoformat()}'
    msg['From']=os.getenv('SMTP_FROM',user);msg['To']=to
    msg.set_content('\n'.join(f'{o.customer.name} | {o.order_no} | {o.product_name} | {o.remaining_dispatch} undelivered | {o.expected_date} | {o.days_left} days left' for o in outstanding))
    port=int(os.getenv('SMTP_PORT','587'))
    context=ssl.create_default_context()
    transport=smtplib.SMTP_SSL(host,port,timeout=30,context=context) if port==465 else smtplib.SMTP(host,port,timeout=30)
    with transport as smtp:
        if port!=465: smtp.starttls(context=context)
        smtp.login(user,password);smtp.send_message(msg)
    for o in outstanding:
        db.session.add(ReminderLog(order_id=o.id,reminder_date=today,days_left=o.days_left,channel='email',status='sent'))
    db.session.commit()
    return {"sent_orders": len(outstanding)}

@app.cli.command('send-delivery-reminders')
def delivery_reminders_command():
    print(send_delivery_reminders())

@app.get('/api/cron/delivery-reminders')
def delivery_reminders_cron():
    secret=os.getenv('CRON_SECRET')
    if not secret or not secrets.compare_digest(request.headers.get('Authorization',''), 'Bearer '+secret):
        abort(401)
    if not os.getenv('ALERT_EMAIL_TO'):
        return {'status':'disabled','reason':'ALERT_EMAIL_TO not configured'}
    try:
        return send_delivery_reminders()
    except Exception:
        db.session.rollback()
        app.logger.exception('Delivery reminder job failed')
        return {"error": "Delivery reminder job failed"}, 500

@app.get("/health")
def health(): return {"status":"ok"},200

@app.cli.command("init-db")
def init_db():
    db.create_all()
    print("Database tables initialized. If upgrading an existing database, run flask migrate-db.")

@app.cli.command("create-admin")
def create_admin():
    import getpass
    username=input("Admin username: ").strip()
    password=getpass.getpass("Strong password (12+ characters): ")
    if not username or len(password)<12: raise RuntimeError("Username and password of 12+ characters required")
    if User.query.filter_by(username=username).first(): raise RuntimeError("Username already exists")
    db.session.add(User(username=username,name="Administrator",role="Admin",password_hash=generate_password_hash(password)))
    db.session.commit()
    print("Admin created")

if __name__=="__main__":
    app.run(host="127.0.0.1",port=int(os.getenv("PORT",5000)),debug=False)
