"""
BGPDATA - BGP Data Collection and Analytics Service

This software is part of the BGPDATA project, which is designed to collect, process, and analyze BGP data from various sources.
It helps researchers and network operators get insights into their network by providing a scalable and reliable way to analyze and inspect historical and live BGP data from Route Collectors around the world.

Author: Robin Röper

© 2024 BGPDATA. All rights reserved.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:
1. Redistributions of source code must retain the above copyright notice, this list of conditions, and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions, and the following disclaimer in the documentation and/or other materials provided with the distribution.
3. Neither the name of BGPDATA nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""
from flask import Flask, render_template, request, redirect, url_for, abort, session
from flask_compress import Compress
from flask_cors import CORS
from flask_talisman import Talisman
from asgiref.wsgi import WsgiToAsgi
from utils.postmark import postmark
from utils.database import PostgreSQL
from utils.transformers import time_ago, hash_text, format_text, sanitize_text
from utils.validators import is_authenticated, is_onboarded, is_valid_email
from utils.generators import generate_verification_code
from views.user import user_blueprint
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text
import urllib.parse
import importlib
import logging
import asyncio
import atexit
import random
import httpx
import sass  # type: ignore
import pytz
import sys
import os
import re

# Environment variables
SECRET_KEY = os.getenv('SECRET_KEY', 'your-flask-secret-key')
ENVIRONMENT = os.getenv('ENVIRONMENT', 'development')
# Cache for 1 day (86400 seconds)
CACHE_MAX_AGE = int(os.getenv('CACHE_MAX_AGE', '86400'))

def create_app():
    app = Flask(__name__)
    app.secret_key = SECRET_KEY

    # Set secure session cookies
    app.config['SESSION_COOKIE_SECURE'] = ENVIRONMENT == 'production'

    # Logging configuration for Uvicorn and Gunicorn
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s: %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )

    # Ensure the Flask app logger also uses this configuration
    app.logger.setLevel(logging.INFO)

    # Initialize CORS
    cors_origin = [
        'https://bgp-data.net',
        'http://localhost:8080'
    ]

    CORS(
        app,
        resources={r"/api/*": {"origins": cors_origin}},
        supports_credentials=True,
    )

    # Initialize Flask-Talisman
    Talisman(app, content_security_policy=None, force_https=False)
        
    def compile_scss():
        scss_file = 'static/styles/main.scss'
        css_file = 'static/styles/main.css'
        with open(css_file, 'w', -1, 'utf8') as f:
            f.write(sass.compile(filename=scss_file))


    # Compile SCSS once on startup
    compile_scss()

    # Compress Application
    Compress(app)

    # Enable Cache Control after each request in production mode
    if ENVIRONMENT == 'production':
        @app.after_request
        def add_header(response):
            if request.path.startswith('/static/'):
                response.cache_control.max_age = CACHE_MAX_AGE
                response.cache_control.no_cache = None
                response.cache_control.public = True
            return response
        
    # Compile SCSS before each request in development mode
    if ENVIRONMENT == 'development':
        @app.before_request
        def before_request():
            compile_scss()

    # Scheduler to have soft deleted or not onboarded users eventually purged from the system
    #scheduler = BackgroundScheduler()
    #scheduler.add_job(
    #    func=lambda: db.users.delete_many({"$or": [{"is_deleted": True}, {"is_onboarded": False}], "updated_at": {
    #                                      "$lt": datetime.now(pytz.utc) - timedelta(days=180)}}),
    #    trigger='interval',
    #    days=1  # run every day
    #)
    #scheduler.start()

    # Shut down the scheduler when exiting the app
    #atexit.register(scheduler.shutdown)

    """
    Jinja
    """

    app.jinja_env.filters['time_ago'] = time_ago
    app.jinja_env.filters['format'] = format_text
    app.jinja_env.filters['sanitize'] = sanitize_text

    """
    Authentication
    """


    @app.route('/')
    def index():
        return render_template('pages/index.html')


    @app.route('/as/<int:asn>')
    async def asn(asn):
        try:
            # Retrieve ASN name
            async with httpx.AsyncClient() as client:
                response = await client.get(f"https://stat.ripe.net/data/as-names/data.json?resource=AS{asn}", timeout=10)
                response.raise_for_status()
                as_name = response.json()['data']['names'].get(str(asn), "Unknown")
    #
            prefix = "2001:67c:2e8::/48"
            query = text(f"SELECT * FROM ris_lite WHERE prefix = '{prefix}' ORDER BY timestamp DESC LIMIT 10000")
            
            # Retrieve Routing History from Local
            async with PostgreSQL() as session:
                result = await session.execute(query)
                rows = result.fetchall()
                
            two_hops=False

            # Organize by origin and prefix
            by_origin = {}
            for row in rows:
                prefix = row['prefix']
                timestamp = row['timestamp']
                full_peer_count = row['full_peer_count']
                path = row['segment'].split(',')

                if prefix == "0/0" or prefix == "::/0":  # Ignore default routes
                    continue

                if full_peer_count < 1:  # Apply low peer visibility filter
                    continue

                origin = path[-1]
                if two_hops and len(path) > 1:
                    first_hop = path[-2]
                    origin_tuple = (first_hop, origin)
                else:
                    origin_tuple = (origin,)

                # Organize by origin and prefix
                by_origin.setdefault(origin_tuple, {})
                by_origin[origin_tuple].setdefault(prefix, [])
                by_origin[origin_tuple][prefix].append((timestamp, full_peer_count))

            app.logger.info(f"Local ris_lite data for AS{asn}: {by_origin}")

            # Retrieve Routing History from RIPEstat
            async with httpx.AsyncClient() as client:
                response = await client.get(f"https://stat.ripe.net/data/routing-history/data.json?resource={prefix}", timeout=10)
                response.raise_for_status()
                ripestat_results = response.json()['data']['by_origin']
            
            app.logger.info(f"RIPEstat routing history for AS{asn}: {ripestat_results}")

        except Exception as e:
            app.logger.error(f"Failed to retrieve AS{asn}: {str(e)}")
            return abort(500, description="An error occurred")

        return render_template('pages/asn.html', asn=asn, as_name=as_name)


    #@app.route('/logout')
    #def logout():
    #    try:
    #        if not is_authenticated():
    #            return redirect(url_for('index'))
    #
    #        session.clear()
    #
    #    except Exception as e:
    #        app.logger.error("Failed to log out: %s", str(e))
    #        return abort(500, description="An error occurred")
    #
    #    return redirect(url_for('index'))
    #
    #
    #@app.route('/forget', methods=["POST"])
    #def forget():
    #    try:
    #        if not is_authenticated():
    #            return redirect(url_for('index'))
    #
    #        db.posts.delete_many({"author_id": ObjectId(session['user_id'])})
    #        db.comments.delete_many({"author_id": ObjectId(session['user_id'])})
    #        db.users.update_one(
    #            {"_id": ObjectId(session['user_id'])}, {"$set": {
    #                # Delete personal identifying data
    #                "email": None,
    #                "first_name": None,
    #                "last_name": None,
    #                "company": None,
    #                "study_id": None,
    #                "avatar": None,
    #                "groups": [],
    #                "notifications": [],
    #                "is_onboarded": False,
    #                "is_deleted": True,
    #                "updated_at": datetime.now(pytz.utc)
    #            }})
    #
    #        session.clear()
    #
    #    except Exception as e:
    #        app.logger.error("Failed to delete account: %s", str(e))
    #        return abort(500, description="An error occurred")
    #
    #    return redirect(url_for('index'))
    #
    #
    #@app.route('/verify')
    #def verify():
    #    try:
    #        if is_authenticated():
    #            return redirect(url_for('feed'))
    #
    #        email = str(request.values.get("email")).lower()
    #        code = str(request.values.get("code"))
    #
    #        users = list(db.users.find(
    #            {'is_onboarded': True, 'is_deleted': False, 'is_suspended': False}).limit(5))
    #        user_count = db.users.count_documents(
    #            {'is_onboarded': True, 'is_deleted': False})
    #
    #        if not email or not is_valid_email(email):
    #            app.logger.warning("Invalid email address: %s", email)
    #            return abort(400, description="Invalid email address format")
    #        else:
    #            # Determine the type based on email domain
    #            email_domain = email.rsplit('@', maxsplit=1)[-1]
    #            email_providers = {
    #                'gmail.com': {'name': 'GMail', 'link': 'mail.google.com'},
    #                'fom-net.de': {'name': 'FOM ADFS', 'link': 'adfs.fom-net.de'},
    #                'yahoo.com': {'name': 'Yahoo Mail', 'link': 'mail.yahoo.com'},
    #                'hotmail.de': {'name': 'Outlook', 'link': 'outlook.live.com'},
    #                'mailfence.com': {'name': 'Mailfence', 'link': 'mailfence.com/sw?type=L&state=0&lf=mailfence'}
    #            }
    #
    #            user_type = 'student' if email_domain == 'fom-net.de' else 'alumni'
    #
    #            user_provider = email_providers.get(
    #                email_domain, {'name': 'Mailbox', 'link': email_domain},
    #            )
    #
    #        if code != "None":
    #            user = db.users.find_one(
    #                {"email_digest": hash_text(email), "code_digest": hash_text(code)})
    #
    #            if user:
    #                # Check whether the code is already expired
    #                code_expiry = user.get("code_expiry")
    #
    #                if isinstance(code_expiry, str):
    #                    code_expiry = datetime.strptime(
    #                        code_expiry, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=pytz.utc)
    #                elif code_expiry.tzinfo is None:
    #                    # If code_expiry is naive, make it timezone-aware by setting it to UTC
    #                    code_expiry = code_expiry.replace(tzinfo=pytz.utc)
    #
    #                if datetime.now(pytz.utc) > code_expiry:
    #                    # Code is already expired
    #                    return redirect(f'/verify?email={email}')
    #                else:
    #                    # Code is valid and not expired
    #                    session['user_id'] = str(user.get('_id', None))
    #                    session['email'] = email
    #                    session['expiry'] = (datetime.now(
    #                        pytz.utc) + timedelta(days=90)).strftime('%Y-%m-%dT%H:%M:%SZ')
    #
    #                    db.users.update_one(
    #                        {"_id": user["_id"]}, {"$set": {
    #                            "code_expiry": datetime.now(pytz.utc),
    #                            "updated_at": datetime.now(pytz.utc)
    #                        }})
    #
    #                    return redirect(url_for('feed'))
    #            else:
    #                # Incorrect code
    #                return redirect(f'/verify?email={email}')
    #        elif db.users.find_one(
    #                {"email_digest": hash_text(email), "code_expiry": {"$gte": datetime.now(pytz.utc)}}):
    #            app.logger.debug(
    #                "Found existing account with non-expired code: %s", email)
    #        else:
    #            # Generate a random 5-digit mixed numeric ascii code
    #            code = generate_verification_code()
    #            code_digest = hash_text(code)
    #            code_expiry = datetime.now(pytz.utc) + timedelta(minutes=15)
    #
    #            # Generate random color for user
    #            random_color = random.choice(
    #                [
    #                    "#07bba8",
    #                    "#a258cd",
    #                    "#3faac1",
    #                ])
    #
    #            if db.users.find_one({"email_digest": hash_text(email)}):
    #                app.logger.debug("Found existing account for: %s", email)
    #                db.users.update_one(
    #                    {"email_digest": hash_text(email)}, {"$set": {
    #                        "code_digest": code_digest,
    #                        "code_expiry": code_expiry,
    #                        "is_deleted": False,
    #                        "updated_at": datetime.now(pytz.utc)
    #                    }})
    #            else:
    #                app.logger.debug("Creating new account for: %s", email)
    #                db.users.insert_one({
    #                    "email": email,
    #                    "email_digest": hash_text(email),
    #                    "code_digest": code_digest,
    #                    "code_expiry": code_expiry,
    #                    "user_type": user_type,
    #                    "first_name": None,
    #                    "last_name": None,
    #                    "company": None,
    #                    "study_id": None,
    #                    "avatar": None,
    #                    "color": random_color,
    #                    "is_onboarded": False,
    #                    "is_suspended": False,
    #                    "is_priviliged": False,
    #                    "is_verified": user_type == 'student',
    #                    "is_deleted": False,
    #                    "is_mailable": True,
    #                    "groups": [],
    #                    "created_at": datetime.now(pytz.utc),
    #                    "updated_at": datetime.now(pytz.utc)
    #                })
    #
    #            subject = "Your FOMBook Login Link"
    #            message = f"""<p>Hey there,</p>
    #                        <p>To finish logging into your account, please click the following link:</p>
    #                        <p><a href="{HOST}/verify?email={urllib.parse.quote(email)}&code={code}">Login to FOMBook</a></p>
    #                        <p>Or enter the following verification code manually:</p>
    #                        <p><strong>{code}</strong></p>"""
    #
    #            try:
    #                app.logger.debug(
    #                    "Sending Email with Login Code: %s", code)
    #                postmark.emails.send(
    #                    From='help@fombook.com',
    #                    To=email,
    #                    Subject=subject,
    #                    HtmlBody=message,
    #                    TextBody=message
    #                )
    #            except Exception as e:
    #                app.logger.error("Failed to send email: %s", str(e))
    #
    #    except Exception as e:
    #        app.logger.error("Failed to retrieve verify: %s", str(e))
    #        return abort(500, description="An error occurred")
    #
    #    return render_template('verify.html', users=users, user_count=user_count, email=email, user_provider=user_provider)
    #
    #
    #"""
    #Onboarding
    #"""
    #
    #
    #@ app.route("/onboarding")
    #def onboarding():
    #    try:
    #        if not is_authenticated():
    #            return redirect(url_for('index'))
    #
    #        if is_onboarded():
    #            return redirect(url_for('feed'))
    #
    #        profile = db.users.find_one({'_id': ObjectId(session['user_id'])})
    #        studies = list(db.studies.find({}))
    #
    #    except Exception as e:
    #        app.logger.error("Failed to load onboarding: %s", str(e))
    #        return abort(500, description="An error occurred")
    #
    #    return render_template('onboarding.html', profile=profile, studies=studies)

    # Register Blueprints
    #app.register_blueprint(user_blueprint, url_prefix='/user')

    return app

if __name__ == '__main__':
    app = create_app()
    app.run()