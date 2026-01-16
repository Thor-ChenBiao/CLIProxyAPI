"""
Page routes for Key Portal web interface.
"""

from flask import render_template
import config as portal_config


def register_page_routes(app):
    """Register all page routes."""

    @app.route("/")
    def index():
        """Tutorial page showing how to use the service."""
        return render_template("index.html", service_info=portal_config.SERVICE_INFO)

    @app.route("/register")
    def register_page():
        """User registration page."""
        return render_template("register.html")

    @app.route("/my-keys")
    def my_keys_page():
        """User's keys management page."""
        return render_template("my_keys.html")

    @app.route("/admin/users")
    def admin_users_page():
        """Admin page for user statistics."""
        return render_template("admin_users.html")

    @app.route("/login")
    def login():
        """OAuth login page for contributing keys."""
        return render_template("login.html")

    @app.route("/status")
    def status():
        """Key status page showing all registered keys."""
        return render_template("status.html")
