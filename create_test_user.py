#!/usr/bin/env python
"""Bootstrap the admin account and permission group on first container start.

On a fresh database this creates the initial `admin` superuser with a RANDOM,
one-time password and prints it clearly so the operator can capture it from the
container logs. It never overwrites an existing admin's password. There is no
hardcoded default password — nothing to guess on a public deployment.
"""
import os
import secrets
import django

# Setup Django environment
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project.settings")
django.setup()

from django.contrib.auth.models import User, Group, Permission
from django.contrib.contenttypes.models import ContentType

ADMIN_USERNAME = "admin"


def setup_commoneers_group():
    """Create 'commoneers' group and assign permissions if not exists."""
    group_name = "commoneers"
    group, created = Group.objects.get_or_create(name=group_name)
    if created:
        print(f"✅ Created group: {group_name}")
    else:
        print(f"✅ Group already exists: {group_name}")

    # Assign permissions for device assignments, Shelly devices, and electricity prices
    models_and_perms = [
        ("deviceassignment", ["add_deviceassignment", "change_deviceassignment", "delete_deviceassignment", "view_deviceassignment"]),
        ("shellydevice", ["add_shellydevice", "change_shellydevice", "delete_shellydevice", "view_shellydevice"]),
        ("electricityprice", ["view_electricityprice"]),
    ]
    for model, perm_codenames in models_and_perms:
        ct = ContentType.objects.get(app_label="app", model=model)
        for codename in perm_codenames:
            try:
                perm = Permission.objects.get(content_type=ct, codename=codename)
                group.permissions.add(perm)
                print(f"  - Added permission: {codename} to group {group_name}")
            except Permission.DoesNotExist:
                print(f"  - Permission not found: {codename} for model {model}")

def _reset_hint() -> str:
    return (
        "  To reset it later, run on the host:\n"
        "    docker exec -it django-shelly-prod python manage.py changepassword admin\n"
        "  (podman: swap 'docker' for 'podman')"
    )


def ensure_admin_user():
    """Ensure an 'admin' superuser exists; on first creation use a random password.

    No hardcoded default password is ever set, so a fresh public deployment has
    no guessable admin. The generated password is printed once — capture it from
    the container logs, then log in and change it.
    """
    admin_user = User.objects.filter(username=ADMIN_USERNAME).first()
    if admin_user:
        print(f"✅ Admin user '{ADMIN_USERNAME}' already exists — password left unchanged.")
        print(_reset_hint())
        return

    # secrets.token_urlsafe(18) -> ~24 chars, URL-safe, high entropy.
    password = secrets.token_urlsafe(18)
    User.objects.create_superuser(ADMIN_USERNAME, "", password)

    # Fixed sentinels so the install scripts can extract this block from logs.
    print(">>>>> INITIAL ADMIN CREDENTIALS >>>>>")
    print(f"  username: {ADMIN_USERNAME}")
    print(f"  password: {password}")
    print("")
    print("  This password is shown ONCE. Log in, then change it.")
    print(_reset_hint())
    print("<<<<< END INITIAL ADMIN CREDENTIALS <<<<<")


if __name__ == "__main__":
    setup_commoneers_group()
    ensure_admin_user()
